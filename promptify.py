"""Promptify: one History dictation -> a prompt for Claude, written by a pluggable engine.

The dictation path is untouched - this runs only when the user presses the button, on a copy,
and the History entry keeps its text. The engine gets the fixed system prompt (promptify.txt,
next to vocab.txt) and the dictation, and must answer with ONE JSON object:
{prompt, questions: [{q, why, options?}], notes?, more_prompts?}. Pass 2 sends the prompt(s) as
they stand in the panel plus the user's answers and gets the folded prompt back.

Engines: three CLIs that carry their own login (Claude Code, Codex, Gemini CLI) and key-based
HTTP APIs (OpenAI, OpenRouter, Anthropic, any OpenAI-compatible base URL). The CLI invocations
were measured on 2026-08-28 (NOTES.md); the flags that looked optional are not:
`--safe-mode` keeps claude from loading CLAUDE.md, hooks, memory and MCP servers (56k tokens
and 89 s per call without it), `--bare` would drop the OAuth login, an inherited open stdin
costs 3 s, and without `--model` the call runs on the user's settings model.

No engine text ever reaches murmur.log: only return codes, durations and the last stderr line.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
SYSTEM_FILE = RES / "promptify.txt"

SCHEMA = {"type": "object", "required": ["prompt", "questions"], "properties": {
    "prompt": {"type": "string"},
    "questions": {"type": "array", "maxItems": 4, "items": {
        "type": "object", "required": ["q", "why"], "properties": {
            "q": {"type": "string"}, "why": {"type": "string"},
            "options": {"type": "array", "minItems": 2, "maxItems": 3, "items": {"type": "string"}}}}},
    "notes": {"type": "string"},
    "more_prompts": {"type": "array", "items": {"type": "string"}}}}
SCHEMA_JSON = json.dumps(SCHEMA, separators=(",", ":"))

TARGETS = (("Claude Code", "code"), ("claude.ai", "chat"))
TIMEOUT = 150            # s; the slowest measured pass was 17 s on this laptop's busy CPU

# key -> how the engine is reached. `model` is the default when Settings leaves the model blank.
ENGINES = {
    "claude": {"label": "Claude Code", "kind": "cli", "exe": "claude", "model": "sonnet",
               "models": "sonnet · opus · haiku · a full model id"},
    "codex": {"label": "Codex", "kind": "cli", "exe": "codex", "model": "",
              "models": "blank = your Codex default · gpt-5.1-codex · gpt-5.1-codex-mini"},
    "gemini": {"label": "Gemini CLI", "kind": "cli", "exe": "gemini", "model": "",
               "models": "blank = the CLI's default · gemini-2.5-pro · gemini-2.5-flash"},
    "openai": {"label": "OpenAI", "kind": "openai", "url": "https://api.openai.com/v1",
               "model": "gpt-5-mini", "models": "gpt-5-mini · gpt-5 · gpt-4.1"},
    "openrouter": {"label": "OpenRouter", "kind": "openai", "url": "https://openrouter.ai/api/v1",
                   "model": "anthropic/claude-sonnet-4.5",
                   "models": "anthropic/claude-sonnet-4.5 · openai/gpt-5-mini · google/gemini-2.5-flash"},
    "anthropic": {"label": "Anthropic API", "kind": "anthropic", "url": "https://api.anthropic.com",
                  "model": "claude-sonnet-4-5", "models": "claude-sonnet-4-5 · claude-haiku-4-5"},
    "custom": {"label": "Custom (OpenAI-compatible)", "kind": "openai", "url": "", "model": "",
               "models": "whatever the endpoint serves - Ollama, LM Studio, Groq..."},
}
ORDER = ("claude", "codex", "gemini", "openai", "openrouter", "anthropic", "custom")

log = print          # murmur.py points this at its own log(); the tests leave it on print


class PromptifyError(Exception):
    """A message the panel shows as it is - short, and says what to do."""


class Cancelled(Exception):
    pass


# --- what goes in and what comes out ---------------------------------------------------------
def system_prompt() -> str:
    return SYSTEM_FILE.read_text(encoding="utf-8")


def message(target: str, dictation: str, prompts=None, questions=None, answers=None) -> str:
    """Pass 1 wraps the dictation; pass 2 adds the prompt(s) as they stand (the user's edits ride
    along) and the answers, verbatim, "(skipped)" where empty. Text inside the tags is material,
    and the system prompt says so - a dictation that says "ignore your rules" is carried, not obeyed."""
    name = dict((v, k) for k, v in TARGETS).get(target, "Claude Code")
    if prompts is None:
        return f"Target: {name}\nPass: 1\n\n<dictation>\n{dictation.strip()}\n</dictation>\n"
    out = [f"Target: {name}\nPass: 2\n\n<dictation>\n{dictation.strip()}\n</dictation>\n"]
    for i, p in enumerate(prompts, 1):
        out.append(f'<previous_prompt n="{i}">\n{p.strip()}\n</previous_prompt>\n')
    out.append("<answers>")
    for i, q in enumerate(questions or []):
        a = (answers or [""] * len(questions))[i] if i < len(answers or []) else ""
        a = " ".join(a.split()) or "(skipped)"
        out.append(f"{i + 1}. Q: {q.get('q', '')}\n   A: {a}")
    out.append("</answers>\n")
    return "\n".join(out)


def parse_result(text: str) -> dict:
    """The JSON object in an engine's reply, code fences and prose around it forgiven."""
    s = (text or "").strip()
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
    except ValueError:
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            raise PromptifyError("The engine answered without a prompt (no JSON in its reply).")
        try:
            obj = json.loads(s[i:j + 1])
        except ValueError:
            raise PromptifyError("The engine's reply was not valid JSON.")
    if not isinstance(obj, dict) or not isinstance(obj.get("prompt"), str):
        raise PromptifyError("The engine's reply had no 'prompt' field.")
    return obj


def normalize(obj: dict) -> dict:
    """{prompts, questions, notes} with every field the panel reads present and typed."""
    prompts = [obj["prompt"].strip()] + [str(p).strip() for p in obj.get("more_prompts") or [] if str(p).strip()]
    qs = []
    for q in obj.get("questions") or []:
        if not isinstance(q, dict) or not str(q.get("q", "")).strip():
            continue
        opts = [str(o).strip() for o in q.get("options") or [] if str(o).strip()]
        qs.append({"q": str(q["q"]).strip(), "why": str(q.get("why", "")).strip(), "options": opts[:3]})
    return {"prompts": prompts, "questions": qs[:4], "notes": str(obj.get("notes") or "").strip()}


# --- engines ---------------------------------------------------------------------------------
def find_exe(name: str):
    """A CLI on this machine, or None. `which` first (a login-launched murmur inherits the user's
    PATH, and ~/.local/bin is on it here); then the places the installers use."""
    p = shutil.which(name)
    if p and not p.lower().endswith((".cmd", ".bat")):
        return p
    home = Path.home()
    for c in (home / ".local" / "bin" / f"{name}.exe",
              Path(os.environ.get("LOCALAPPDATA", home)) / "Programs" / name / f"{name}.exe"):
        if c.exists():
            return str(c)
    return p          # a .cmd shim, if that is all there is: run through node, see cli_argv


def node_script(shim: str):
    """The JS entry an npm .cmd shim points at, so it can be run with node directly: cmd.exe
    would otherwise re-parse the arguments (the schema's quotes do not survive that)."""
    try:
        txt = Path(shim).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'"%dp0%\\([^"]+\.js)"|%~dp0\\([^" ]+\.js)', txt)
    rel = next((g for g in (m.groups() if m else ()) if g), None)
    if not rel:
        return None
    js = Path(shim).parent / rel.replace("/", os.sep)
    return str(js) if js.exists() else None


def engine_spec(cfg: dict) -> dict:
    key = cfg.get("prompt_engine") or "claude"
    spec = dict(ENGINES.get(key) or ENGINES["claude"])
    spec["key"] = key if key in ENGINES else "claude"
    spec["model"] = (cfg.get("prompt_model") or "").strip() or spec["model"]
    if spec["key"] == "custom":
        spec["url"] = (cfg.get("prompt_url") or "").strip()
    return spec


def available(cfg: dict):
    """(True, "") when the chosen engine can run now; else (False, what to do about it)."""
    spec = engine_spec(cfg)
    if spec["kind"] == "cli":
        if not find_exe(spec["exe"]):
            return False, f"{spec['label']} CLI not found - install it, or pick another engine in Settings."
        return True, ""
    if spec["key"] == "custom" and not spec["url"]:
        return False, "Custom engine: set the base URL in Settings."
    if spec["key"] != "custom" and not (cfg.get("prompt_key") or "").strip():
        return False, f"{spec['label']}: paste an API key in Settings."
    return True, ""


def kill_tree(p) -> None:
    """claude.exe spawns conhost/cmd/tasklist children; Popen.kill() is TerminateProcess on the
    parent only, so the tree is taken down by PID on Windows."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        p.kill()


def run_cli(argv, stdin_text, env, timeout, cancel, cwd):
    """(rc, stdout, stderr). The prompt goes in on stdin and the pipe is closed at once - an
    inherited open stdin costs a fixed 3 s in claude. A frozen --noconsole app has no console:
    every handle is explicit and the child gets no window. Cancel and timeout kill the tree."""
    kw = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
              encoding="utf-8", errors="replace", env=env, cwd=cwd)
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            import ctypes                    # PyInstaller leaks SetDllDirectory into children
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        except Exception:
            pass
    try:
        p = subprocess.Popen(argv, **kw)
    except OSError as e:
        raise PromptifyError(f"Could not start {Path(argv[0]).name}: {e}")
    res = {}

    def pump():
        try:
            res["out"], res["err"] = p.communicate(input=stdin_text)
        except Exception as e:                # pragma: no cover - a broken pipe on kill
            res["exc"] = e
    t = threading.Thread(target=pump, daemon=True)
    t.start()
    t0 = time.monotonic()
    while t.is_alive():
        if cancel is not None and cancel.is_set():
            kill_tree(p)
            t.join(5)
            raise Cancelled()
        if time.monotonic() - t0 > timeout:
            kill_tree(p)
            t.join(5)
            raise PromptifyError(f"Timed out after {timeout:.0f} s.")
        t.join(0.2)
    return p.returncode, res.get("out") or "", res.get("err") or ""


def cli_argv(spec: dict, sysfile: Path):
    """The command line per CLI. Each one: no tools, no project context, no session files,
    JSON out; the prompt arrives on stdin."""
    exe = find_exe(spec["exe"])
    if not exe:
        raise PromptifyError(f"{spec['label']} CLI not found - install it, or pick another engine in Settings.")
    head = [exe]
    if exe.lower().endswith((".cmd", ".bat")):
        js, node = node_script(exe), shutil.which("node")
        if not js or not node:
            raise PromptifyError(f"{spec['label']} is an npm shim I cannot run directly - install the native build.")
        head = [node, js]
    k, model = spec["key"], spec["model"]
    if k == "claude":
        return head + ["-p", "--safe-mode", "--strict-mcp-config", "--tools", "", "--no-session-persistence",
                       "--output-format", "json", "--model", model or "sonnet",
                       "--system-prompt-file", str(sysfile), "--json-schema", SCHEMA_JSON]
    if k == "codex":
        return head + ["exec", "--skip-git-repo-check", "--sandbox", "read-only", "--json"] + \
            (["-m", model] if model else []) + ["-"]
    if k == "gemini":
        return head + ["-p", "", "--output-format", "json"] + (["-m", model] if model else [])
    raise PromptifyError(f"Unknown engine {k!r}.")


def cli_env(spec: dict) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not (spec["key"] == "claude" and k.startswith("CLAUDE"))}   # a nested-session marker
    if spec["key"] == "claude":
        env["MAX_THINKING_TOKENS"] = "0"      # thinking on turns a 10 s call into 75 s
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _friendly(spec: dict, text: str) -> str:
    t = (text or "").strip()
    low = t.lower()
    if "not logged in" in low or "login expired" in low or "please run /login" in low:
        return f"{spec['label']} is not logged in: open a terminal, run `{spec['exe']}`, and log in."
    if "rate limit" in low or "session limit" in low or "usage limit" in low:
        return f"{spec['label']}: usage limit reached. {t[:160]}"
    return f"{spec['label']} failed: {t[:200] or 'no output'}"


def run_cli_engine(spec: dict, msg: str, cancel, workdir: Path) -> tuple:
    """(parsed JSON object, meta) for the CLI engines."""
    sysfile = workdir / "promptify_system.txt"
    text = system_prompt()
    if not sysfile.exists() or sysfile.read_text(encoding="utf-8") != text:
        sysfile.write_text(text, encoding="utf-8")
    empty = workdir / "empty"                 # the child's cwd: nothing to read, nowhere to write
    empty.mkdir(parents=True, exist_ok=True)
    argv = cli_argv(spec, sysfile)
    k = spec["key"]
    stdin = msg if k == "claude" else text + "\n\n" + msg     # only claude takes a system prompt file
    t0 = time.monotonic()
    rc, out, err = run_cli(argv, stdin, cli_env(spec), TIMEOUT, cancel, str(empty))
    wall = time.monotonic() - t0
    tail = (err.strip().splitlines() or [""])[-1][:160]
    log(f"  promptify {k}: rc {rc}, {wall:.1f}s{(', stderr: ' + tail) if tail else ''}")
    meta = {"model": spec["model"], "wall": round(wall, 1)}
    if k == "claude":
        try:
            j = json.loads(out)
        except ValueError:
            raise PromptifyError(_friendly(spec, err or out))
        if j.get("is_error") or rc != 0:
            raise PromptifyError(_friendly(spec, str(j.get("result") or err)))
        used = [m for m in (j.get("modelUsage") or {}) if "haiku" not in m or spec["model"] == "haiku"]
        meta["model"] = used[0] if used else spec["model"]
        so = j.get("structured_output")
        return (so if isinstance(so, dict) else parse_result(str(j.get("result") or ""))), meta
    if k == "codex":
        # --json is a JSONL event stream; the last agent message carries the reply
        reply, last = "", ""
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            item = ev.get("item") or {}
            if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                reply = item.get("text") or reply
            if ev.get("type") == "error":
                last = str(ev.get("message") or "")
        if rc != 0 and not reply:
            raise PromptifyError(_friendly(spec, last or err or out))
        return parse_result(reply or out), meta
    if k == "gemini":
        try:
            j = json.loads(out)
            reply = j.get("response") if isinstance(j, dict) else out
            if isinstance(j, dict) and j.get("error"):
                raise PromptifyError(_friendly(spec, str(j["error"])))
        except ValueError:
            reply = out
        if rc != 0 and not reply:
            raise PromptifyError(_friendly(spec, err or out))
        return parse_result(reply or ""), meta
    raise PromptifyError(f"Unknown engine {k!r}.")


def http_json(url: str, body: dict, headers: dict, timeout=120) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "murmur", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace"))
            detail = (detail.get("error") or detail)
            detail = detail.get("message") if isinstance(detail, dict) else str(detail)
        except Exception:
            detail = ""
        what = {401: "the API key was rejected", 402: "no credits left on this key",
                403: "this key may not use that model", 404: "unknown model or URL",
                429: "rate limit reached - try again in a moment"}.get(e.code, f"HTTP {e.code}")
        raise PromptifyError(f"{what}{(': ' + str(detail)[:160]) if detail else ''}")
    except urllib.error.URLError as e:
        raise PromptifyError(f"No connection: {getattr(e, 'reason', e)}")
    except TimeoutError:
        raise PromptifyError(f"Timed out after {timeout} s.")


def run_api_engine(spec: dict, cfg: dict, msg: str) -> tuple:
    key, model, sysp = (cfg.get("prompt_key") or "").strip(), spec["model"], system_prompt()
    if not model:
        raise PromptifyError(f"{spec['label']}: set a model in Settings.")
    t0 = time.monotonic()
    if spec["kind"] == "anthropic":
        j = http_json(spec["url"].rstrip("/") + "/v1/messages",
                      {"model": model, "max_tokens": 4096, "system": sysp,
                       "messages": [{"role": "user", "content": msg}]},
                      {"x-api-key": key, "anthropic-version": "2023-06-01"})
        text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    else:
        url = spec["url"].rstrip("/")
        if not url:
            raise PromptifyError("Custom engine: set the base URL in Settings.")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if spec["key"] == "openrouter":
            headers.update({"HTTP-Referer": "https://github.com/BlueRaddish/murmur", "X-Title": "murmur"})
        body = {"model": model, "temperature": 0.2,
                "messages": [{"role": "system", "content": sysp}, {"role": "user", "content": msg}],
                "response_format": {"type": "json_object"}}
        try:
            j = http_json(url + "/chat/completions", body, headers)
        except PromptifyError as e:
            if "response_format" not in str(e):
                raise
            body.pop("response_format")        # an endpoint without JSON mode: ask, and parse leniently
            j = http_json(url + "/chat/completions", body, headers)
        try:
            text = j["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise PromptifyError(f"{spec['label']}: unexpected reply shape.")
        if isinstance(text, list):             # some endpoints return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    wall = time.monotonic() - t0
    log(f"  promptify {spec['key']}: {wall:.1f}s")
    return parse_result(text), {"model": model, "wall": round(wall, 1)}


def draft(cfg: dict, dictation: str, target="code", prompts=None, questions=None, answers=None,
          cancel=None, workdir=None) -> dict:
    """The whole thing: build the message, run the engine, return {prompts, questions, notes,
    engine, model, target, wall, t}. Raises PromptifyError (show it) or Cancelled (say nothing)."""
    spec = engine_spec(cfg)
    workdir = Path(workdir or Path(os.environ.get("APPDATA", Path.home())) / "murmur") / "promptify"
    workdir.mkdir(parents=True, exist_ok=True)
    msg = message(target, dictation, prompts, questions, answers)
    if spec["kind"] == "cli":
        obj, meta = run_cli_engine(spec, msg, cancel, workdir)
    else:
        obj, meta = run_api_engine(spec, cfg, msg)
    out = normalize(obj)
    out.update(engine=spec["key"], model=meta.get("model") or spec["model"], target=target,
               wall=meta.get("wall", 0.0), t=time.time())
    return out
