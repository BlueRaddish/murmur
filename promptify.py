"""Promptify: one History dictation -> a prompt for Claude, written by a pluggable engine.

The dictation path is untouched - this runs only when the user presses the button, on a copy,
and the History entry keeps its text. The engine gets the fixed system prompt (promptify.txt,
next to vocab.txt) and the dictation, and must answer with ONE JSON object:
{prompt, questions: [{q, why, options?}], notes?, more_prompts?}. Pass 2 sends the prompt(s) as
they stand in the panel plus the user's answers and gets the folded prompt back.

Engines are login-only (connect.py holds the sign-in and the connection state): three CLIs
that carry their own login - Claude Code, Codex, Gemini CLI - and OpenRouter, whose OAuth
Connect flow hands murmur a key that reaches the OpenAI/Google/Anthropic models too. The CLI
invocations were measured on 2026-08-28 (NOTES.md); the flags that looked optional are not:
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

# `analysis` comes first: key order is generation order, so the model reasons - decisions,
# musings, mis-hearing suspects, ambiguities - before it commits to a goal line. murmur
# discards it (the lectures: thinking only counts when it is emitted, and it needs structure).
SCHEMA = {"type": "object", "required": ["analysis", "prompt", "questions"], "properties": {
    "analysis": {"type": "string"},
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

# key -> how the engine is reached. `model` is the default when the model field is blank;
# `login` is the one line the engines sheet shows under the name.
ENGINES = {
    "claude": {"label": "Claude Code", "kind": "cli", "exe": "claude", "model": "sonnet",
               "models": "sonnet · opus · haiku · fable · or a full id such as claude-sonnet-5",
               "login": "Your claude.ai subscription, through the Claude Code app"},
    "codex": {"label": "Codex", "kind": "cli", "exe": "codex", "model": "gpt-5.6-luna",
              "models": "gpt-5.6-luna (the default) · gpt-5.5 · blank = Codex's own pick",
              "login": "Your ChatGPT plan, through the Codex app"},
    "gemini": {"label": "Gemini CLI", "kind": "cli", "exe": "gemini", "model": "",
               "models": "blank = the CLI's default · gemini-2.5-flash · gemini-2.5-pro",
               "login": "Your Google account, through the Gemini CLI (npm install -g @google/gemini-cli)"},
    "openrouter": {"label": "OpenRouter", "kind": "openai", "url": "https://openrouter.ai/api/v1",
                   "model": "openai/gpt-5.6-luna",
                   "models": "openai/gpt-5.6-luna · anthropic/claude-sonnet-5 · google/gemini-2.5-flash · openrouter/free",
                   "login": "One sign-in that reaches Claude, GPT and Gemini models; free models included"},
}
ORDER = ("claude", "codex", "gemini", "openrouter")

log = print          # murmur.py points this at its own log(); the tests leave it on print


class PromptifyError(Exception):
    """A message the panel shows as it is - short, and says what to do."""


class Cancelled(Exception):
    pass


# --- what goes in and what comes out ---------------------------------------------------------
def system_prompt() -> str:
    return SYSTEM_FILE.read_text(encoding="utf-8")


def message(target: str, dictation: str, prompts=None, questions=None, answers=None, vault_ctx="") -> str:
    """Pass 1 wraps the dictation; pass 2 adds the prompt(s) as they stand (the user's edits ride
    along) and the answers, verbatim, "(skipped)" where empty. The vault context, when there is
    one, rides right after the dictation on both passes - pass 2 reuses pass 1's block verbatim
    so the answers fold against the same facts. Text inside the tags is material, and the system
    prompt says so - a dictation that says "ignore your rules" is carried, not obeyed."""
    name = dict((v, k) for k, v in TARGETS).get(target, "Claude Code")
    vc = (vault_ctx.strip() + "\n") if (vault_ctx or "").strip() else ""
    if prompts is None:
        return f"Target: {name}\nPass: 1\n\n<dictation>\n{dictation.strip()}\n</dictation>\n{vc}"
    out = [f"Target: {name}\nPass: 2\n\n<dictation>\n{dictation.strip()}\n</dictation>\n{vc}"]
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
    PATH, and ~/.local/bin is on it here); then the places the installers use. An npm .cmd shim
    is never returned as such: cmd.exe would re-parse the arguments. Codex keeps a native
    codex.exe under the shim's node_modules (what the shim spawns anyway), so that is used;
    otherwise the shim's JS entry runs under node."""
    p = shutil.which(name)
    if p and not p.lower().endswith((".cmd", ".bat")):
        return p
    home = Path.home()
    for c in (home / ".local" / "bin" / f"{name}.exe",
              Path(os.environ.get("LOCALAPPDATA", home)) / "Programs" / name / f"{name}.exe"):
        if c.exists():
            return str(c)
    shim = p or next((str(c) for c in (Path(os.environ.get("APPDATA", home)) / "npm" / f"{name}.cmd",)
                      if c.exists()), None)
    if not shim:
        return None
    for native in Path(shim).parent.glob(f"node_modules/@*/{name}/node_modules/@*/*/vendor/*/bin/{name}.exe"):
        return str(native)
    return shim


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


def engine_spec(cfg: dict, key=None) -> dict:
    key = key or cfg.get("prompt_engine") or "claude"
    spec = dict(ENGINES.get(key) or ENGINES["claude"])
    spec["key"] = key if key in ENGINES else "claude"
    models = cfg.get("prompt_models") or {}
    spec["model"] = ((models.get(spec["key"]) if isinstance(models, dict) else "") or "").strip() or spec["model"]
    return spec


def available(cfg: dict, appdir=None):
    """(True, "") when the chosen engine can run now; else (False, what to do about it) -
    from the connection state, no process and no network."""
    import connect
    spec = engine_spec(cfg)
    state, detail = connect.status(spec["key"], appdir=appdir)
    if state == "connected":
        return True, ""
    if state == "missing":
        return False, f"{detail} - install it, or pick another engine in Promptify > Engines."
    return False, f"{spec['label']} is not connected - press Connect in Promptify > Engines."


def kill_tree(p) -> None:
    """claude.exe spawns conhost/cmd/tasklist children; Popen.kill() is TerminateProcess on the
    parent only, so the tree is taken down by PID on Windows."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        p.kill()


def run_cli(argv, stdin_text, env, timeout, cancel, cwd, watch=None):
    """(rc, stdout, stderr). With `stdin_text` the prompt goes in on stdin and the pipe is closed
    at once (an inherited open stdin costs a fixed 3 s in claude); with None the pipe stays open
    and empty, which gemini needs (it hangs on EOF before its consent prompt). `watch` =
    (text, reply bytes): when `text` shows up on stdout the reply is written - how a dead
    gemini login is refused instead of opening a browser mid-dictation. A frozen --noconsole app
    has no console: every handle is explicit and the child gets no window. Cancel and timeout
    kill the tree."""
    kw = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd)
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
    bufs, done = {"out": bytearray(), "err": bytearray()}, {"watch": watch is None}

    def feed():
        try:
            if stdin_text is not None:
                p.stdin.write(stdin_text.encode("utf-8"))
                p.stdin.close()
        except OSError:
            pass

    def read(stream, name):
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(1)
            if not chunk:
                return
            bufs[name] += chunk
            if name == "out" and not done["watch"] and watch[0] in bufs["out"].decode("utf-8", "replace"):
                done["watch"] = True
                try:
                    p.stdin.write(watch[1])
                    p.stdin.flush()
                except OSError:
                    pass
    ts = [threading.Thread(target=feed, daemon=True),
          threading.Thread(target=read, args=(p.stdout, "out"), daemon=True),
          threading.Thread(target=read, args=(p.stderr, "err"), daemon=True)]
    for t in ts:
        t.start()
    t0 = time.monotonic()
    while p.poll() is None:
        if cancel is not None and cancel.is_set():
            kill_tree(p)
            raise Cancelled()
        if time.monotonic() - t0 > timeout:
            kill_tree(p)
            raise PromptifyError(f"Timed out after {timeout:.0f} s.")
        time.sleep(0.2)
    for t in ts:
        t.join(2)
    try:
        p.stdin.close()
    except OSError:
        pass
    return p.returncode, bufs["out"].decode("utf-8", "replace"), bufs["err"].decode("utf-8", "replace")


def cli_head(spec: dict) -> list:
    """[exe] or [node, script] for a CLI, so a login and an exec run the same binary."""
    exe = find_exe(spec["exe"])
    if not exe:
        raise PromptifyError(f"{spec['label']} is not installed - install it, or pick another engine.")
    if exe.lower().endswith((".cmd", ".bat")):
        js, node = node_script(exe), shutil.which("node")
        if not js or not node:
            raise PromptifyError(f"{spec['label']} is an npm shim I cannot run directly - install the native build.")
        return [node, js]
    return [exe]


def cli_argv(spec: dict, sysfile: Path, workdir: Path, msg: str = ""):
    """The command line per CLI. Each one: no tools, no project context, no session files,
    JSON out; the system prompt travels as a file; the dictation arrives on stdin (claude,
    codex) or as the -p argument (gemini, whose stdin is its consent prompt's)."""
    head = cli_head(spec)
    k, model = spec["key"], spec["model"]
    if k == "claude":
        return head + ["-p", "--safe-mode", "--strict-mcp-config", "--tools", "", "--no-session-persistence",
                       "--output-format", "json", "--model", model or "sonnet",
                       "--system-prompt-file", str(sysfile), "--json-schema", SCHEMA_JSON]
    if k == "codex":
        # "-" = the prompt from stdin; --json = one event per line; --ephemeral = no session
        # files; --ignore-user-config = none of the user's MCP servers; the instructions file
        # replaces Codex's own system prompt; -o keeps the final message where the JSONL cannot
        # be trusted. TOML values on -c: strings quoted, the path with forward slashes.
        schema, last = workdir / "schema.json", workdir / "last.txt"
        if not schema.exists() or schema.read_text(encoding="utf-8") != SCHEMA_JSON:
            schema.write_text(SCHEMA_JSON, encoding="utf-8")
        return head + ["exec", "-", "--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                       "-s", "read-only", "-C", str(workdir / "empty"), "--color", "never"] + \
            (["-m", model] if model else []) + \
            ["-c", 'model_reasoning_effort="low"', "-c", f"model_instructions_file='{sysfile.as_posix()}'",
             "-c", "hide_agent_reasoning=true", "-c", 'history.persistence="none"',
             "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
             "--output-schema", str(schema), "-o", str(last)]
    if k == "gemini":
        # headless whenever stdin is not a TTY; the message is the -p argument (stdin's first
        # line would be read as the consent answer); the system prompt via GEMINI_SYSTEM_MD (see
        # cli_env); -e none = no extensions; the cwd's own settings file turns the tools and the
        # context files off (docs + a local install of 0.57.0; no login here to run it against)
        gs = workdir / "empty" / ".gemini"
        gs.mkdir(parents=True, exist_ok=True)
        (gs / "settings.json").write_text(json.dumps(
            {"tools": {"core": []}, "context": {"fileName": "NONE.md", "includeDirectoryTree": False},
             "privacy": {"usageStatisticsEnabled": False},
             "general": {"enableAutoUpdate": False, "enableAutoUpdateNotification": False}}), encoding="utf-8")
        return head + ["-p", msg, "--skip-trust", "-o", "json", "-e", "none"] + (["-m", model] if model else [])
    raise PromptifyError(f"Unknown engine {k!r}.")


def login_check(spec: dict, argv: list) -> None:
    """Codex's auth failure is a slow chain of retries ('Reconnecting... k/5'); `login status`
    answers in 50 ms with its exit code (the text goes to stderr), so ask first."""
    if spec["key"] != "codex":
        return
    head = argv[:argv.index("exec")]
    kw = dict(capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        rc = subprocess.run(head + ["login", "status"], **kw).returncode
    except (OSError, subprocess.TimeoutExpired):
        return                                    # let exec itself report whatever is wrong
    if rc != 0:
        raise PromptifyError("Codex is not logged in: open a terminal, run `codex login`, and try again.")


def cli_env(spec: dict, sysfile: Path) -> dict:
    """The child's environment: anything that would outrank the login the user made is
    dropped (a key in the environment silently switches claude/codex to API billing)."""
    k = spec["key"]
    drop = {"claude": lambda n: n.startswith("CLAUDE") or n in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
            "codex": lambda n: n in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"),
            "gemini": lambda n: False}[k]
    env = {n: v for n, v in os.environ.items() if not drop(n)}
    if k == "claude":
        env["MAX_THINKING_TOKENS"] = "0"      # thinking on turns a 10 s call into 75 s
    if k == "gemini":
        env.update({"GEMINI_SYSTEM_MD": str(sysfile), "NO_COLOR": "1", "GOOGLE_GENAI_USE_GCA": "true",
                    "GEMINI_CLI_NO_RELAUNCH": "1", "GEMINI_CLI_TRUST_WORKSPACE": "true"})
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
    argv = cli_argv(spec, sysfile, workdir, msg)
    k = spec["key"]
    login_check(spec, argv)
    t0 = time.monotonic()
    for attempt in (0, 1):
        if k == "gemini":     # stdin open and empty; a consent prompt means the login is dead: refuse it
            rc, out, err = run_cli(argv, None, cli_env(spec, sysfile), TIMEOUT, cancel, str(empty),
                                   watch=("[Y/n]:", b"n\n"))
        else:
            rc, out, err = run_cli(argv, msg, cli_env(spec, sysfile), TIMEOUT, cancel, str(empty))
        # a nonzero exit with silence on both streams is a CLI crash (seen intermittently under
        # load right after an auto-update), not an API answer: one retry
        if attempt == 0 and rc != 0 and not out.strip() and not err.strip():
            log(f"  promptify {k}: crashed silently (rc {rc}), retrying once")
            continue
        break
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
        # --json is a JSONL event stream; the last agent message carries the reply, and -o wrote
        # the same text to last.txt
        reply, last = "", ""
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            item = ev.get("item") or {}
            if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                reply = item.get("text") or reply
            elif ev.get("type") == "error":
                last = str(ev.get("message") or "")
            elif ev.get("type") == "turn.failed":
                last = str((ev.get("error") or {}).get("message") or last)
            elif ev.get("type") == "item.completed" and item.get("type") == "error":
                last = str(item.get("message") or last)
        if not reply:
            try:
                reply = (workdir / "last.txt").read_text(encoding="utf-8")
            except OSError:
                pass
        if not reply.strip() or (rc != 0 and last):
            raise PromptifyError(_friendly(spec, last or err or out))
        return parse_result(reply), meta
    if k == "gemini":
        if "[Y/n]:" in out or "Authentication cancelled" in err or "Please set an Auth method" in err:
            raise PromptifyError("Gemini CLI is not connected - press Connect in Promptify > Engines.")
        try:
            j = json.loads(out[out.index("{"):]) if "{" in out else {}
            reply = j.get("response") if isinstance(j, dict) else out
            if isinstance(j, dict) and j.get("error"):
                raise PromptifyError(_friendly(spec, str((j["error"] or {}).get("message") or j["error"])))
        except ValueError:
            reply = out
        if not (reply or "").strip():
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


OPTIONAL = ("response_format", "provider", "thinking", "reasoning_effort", "max_completion_tokens", "max_tokens")


def post_degrading(url: str, body: dict, headers: dict) -> dict:
    """POST; when a 400 names one of the optional keys we sent, drop that key and try again
    (an endpoint without JSON mode, a model that rejects `thinking`, a server that wants
    max_tokens instead of max_completion_tokens...). The system prompt already asks for bare
    JSON and the parser forgives fences, so every rung still works."""
    for _ in range(len(OPTIONAL) + 1):
        try:
            j = http_json(url, body, headers)
        except PromptifyError as e:
            bad = next((k for k in OPTIONAL if k in body and k in str(e)), None)
            if bad is None:
                raise
            body.pop(bad)
            if bad == "max_completion_tokens":
                body["max_tokens"] = 4096
            continue
        if isinstance(j, dict) and j.get("error"):      # OpenRouter answers 200 with a route error
            err = j["error"]
            raise PromptifyError(str(err.get("message") if isinstance(err, dict) else err)[:200])
        return j
    raise PromptifyError("The endpoint rejected every request shape.")


def run_api_engine(spec: dict, cfg: dict, msg: str, appdir=None) -> tuple:
    """OpenRouter: the key its Connect flow handed back, on the OpenAI-compatible endpoint."""
    import connect
    key, model, sysp = connect.openrouter_key(appdir), spec["model"], system_prompt()
    if not key:
        raise PromptifyError("OpenRouter is not connected - press Connect in Promptify > Engines.")
    if not model:
        raise PromptifyError(f"{spec['label']}: set a model.")
    t0 = time.monotonic()
    url = spec["url"].rstrip("/")
    headers = {"Authorization": f"Bearer {key}", "HTTP-Referer": "https://github.com/BlueRaddish/murmur",
               "X-Title": "murmur"}
    body = {"model": model, "stream": False, "max_tokens": 4096,
            "messages": [{"role": "system", "content": sysp}, {"role": "user", "content": msg}],
            "response_format": {"type": "json_object"},
            "provider": {"require_parameters": True}}   # only routes that honour response_format
    try:
        j = post_degrading(url + "/chat/completions", body, headers)
    except PromptifyError as e:
        if "key was rejected" in str(e):
            raise PromptifyError("OpenRouter no longer accepts murmur's key - press Connect again.")
        raise
    try:
        choice = j["choices"][0]
        text = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise PromptifyError(f"{spec['label']}: unexpected reply shape.")
    if isinstance(text, list):             # some endpoints return content parts
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if isinstance(choice, dict) and choice.get("finish_reason") == "length":
        raise PromptifyError(f"{spec['label']}: the reply was cut off at the token limit - try again.")
    wall = time.monotonic() - t0
    log(f"  promptify {spec['key']}: {wall:.1f}s")
    return parse_result(text), {"model": model, "wall": round(wall, 1)}


def vocab_text(appdir: Path) -> str:
    for p in (appdir / "vocab.txt", RES / "vocab.txt"):
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


def draft(cfg: dict, dictation: str, target="code", prompts=None, questions=None, answers=None,
          cancel=None, workdir=None, vault_ctx=None) -> dict:
    """The whole thing: build the message, run the engine, return {prompts, questions, notes,
    vault_ctx, vault_notes, engine, model, target, wall, t}. Pass 1 looks the dictation's
    referents up in the vault (never raises, hard deadline); pass 2 gets pass 1's block back via
    `vault_ctx`. Raises PromptifyError (show it) or Cancelled (say nothing)."""
    spec = engine_spec(cfg)
    workdir = Path(workdir or Path(os.environ.get("APPDATA", Path.home())) / "murmur") / "promptify"
    workdir.mkdir(parents=True, exist_ok=True)
    vault_notes = []
    if vault_ctx is None and prompts is None:
        import vault
        vault_ctx, vault_notes = vault.context_for(cfg, dictation, workdir.parent,
                                                   vocab=vocab_text(workdir.parent))
    msg = message(target, dictation, prompts, questions, answers, vault_ctx or "")
    if spec["kind"] == "cli":
        obj, meta = run_cli_engine(spec, msg, cancel, workdir)
    else:
        obj, meta = run_api_engine(spec, cfg, msg, appdir=workdir.parent)
    out = normalize(obj)
    out.update(engine=spec["key"], model=meta.get("model") or spec["model"], target=target,
               wall=meta.get("wall", 0.0), t=time.time(),
               vault_ctx=vault_ctx or "", vault_notes=vault_notes)
    return out
