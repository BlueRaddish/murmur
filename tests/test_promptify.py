"""promptify: message shapes, lenient parsing, engine argv, and the process runner end to end
against fake CLIs (no network, no login). Run: python tests/test_promptify.py"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import promptify as P

# --- messages --------------------------------------------------------------------------------
m1 = P.message("code", "  fix the thing, you know  ")
assert m1.startswith("Target: Claude Code\nPass: 1\n") and "<dictation>\nfix the thing, you know\n</dictation>" in m1
assert "Target: claude.ai" in P.message("chat", "x") and "Target: Claude Code" in P.message("bogus", "x")
qs = [{"q": "How many?", "why": "count"}, {"q": "Which repo?", "why": "path"}]
m2 = P.message("code", "d", prompts=["P1", "P2"], questions=qs, answers=["four ", ""])
assert 'Pass: 2' in m2 and '<previous_prompt n="1">\nP1\n</previous_prompt>' in m2 and 'n="2">\nP2' in m2
assert "1. Q: How many?\n   A: four" in m2 and "2. Q: Which repo?\n   A: (skipped)" in m2
print("messages ok")

# --- parsing ---------------------------------------------------------------------------------
good = {"prompt": "Do X", "questions": [{"q": "A?", "why": "b", "options": ["1", "2"]}], "notes": "n"}
assert P.parse_result(json.dumps(good)) == good
assert P.parse_result("```json\n" + json.dumps(good) + "\n```") == good
assert P.parse_result("Sure, here it is:\n" + json.dumps(good) + "\nHope that helps") == good
for bad in ("", "no json here", '{"questions": []}', "[1, 2]"):
    try:
        P.parse_result(bad)
        raise AssertionError(bad)
    except P.PromptifyError:
        pass
n = P.normalize({"prompt": " P ", "questions": [{"q": "A?", "why": "b", "options": ["x", "y", "z", "w"]},
                                                 {"q": "", "why": "skip me"}, "junk"],
                 "more_prompts": ["Q", " "], "notes": None})
assert n == {"prompts": ["P", "Q"], "questions": [{"q": "A?", "why": "b", "options": ["x", "y", "z"]}], "notes": ""}
print("parsing ok")

# --- engine spec / availability --------------------------------------------------------------
cfg = {"prompt_engine": "claude", "prompt_model": "", "prompt_key": ""}
assert P.engine_spec(cfg)["model"] == "sonnet" and P.engine_spec({"prompt_engine": "nope"})["key"] == "claude"
assert P.engine_spec({"prompt_engine": "openai", "prompt_model": " gpt-5 "})["model"] == "gpt-5"
assert P.engine_spec({"prompt_engine": "custom", "prompt_url": "http://localhost:11434/v1/"})["url"] == "http://localhost:11434/v1/"
_which = P.find_exe                 # the real lookup would find this machine's claude.exe
P.find_exe = lambda name: None
assert P.available(cfg)[0] is False and "not found" in P.available(cfg)[1]
assert P.available({"prompt_engine": "openai"})[0] is False and "API key" in P.available({"prompt_engine": "openai"})[1]
assert P.available({"prompt_engine": "openai", "prompt_key": "sk-x"}) == (True, "")
assert P.available({"prompt_engine": "custom"})[1].startswith("Custom engine")
assert P.available({"prompt_engine": "custom", "prompt_url": "http://x/v1"}) == (True, "")
assert P.available({"prompt_engine": "gemini_api", "prompt_key": "k"}) == (True, "")
P.find_exe = _which
print("availability ok")

# --- argv per CLI (with a fake exe) ----------------------------------------------------------
tmp = Path(tempfile.mkdtemp())
fake = tmp / "claude.exe"
fake.write_text("")
_find = P.find_exe
P.find_exe = lambda name: str(fake)
sysfile = tmp / "sys.txt"
wd = tmp / "wd"
(wd / "empty").mkdir(parents=True)
a = P.cli_argv(P.engine_spec({"prompt_engine": "claude"}), sysfile, wd)
assert a[:2] == [str(fake), "-p"] and "--safe-mode" in a and "--bare" not in a
assert a[a.index("--model") + 1] == "sonnet" and a[a.index("--json-schema") + 1] == P.SCHEMA_JSON
assert a[a.index("--tools") + 1] == "" and a[a.index("--system-prompt-file") + 1] == str(sysfile)
a = P.cli_argv(P.engine_spec({"prompt_engine": "claude", "prompt_model": "opus"}), sysfile, wd)
assert a[a.index("--model") + 1] == "opus"
a = P.cli_argv(P.engine_spec({"prompt_engine": "codex"}), sysfile, wd)
assert a[1:4] == ["exec", "-", "--json"] and "--ephemeral" in a and "-m" not in a
assert a[a.index("-C") + 1] == str(wd / "empty") and a[a.index("--output-schema") + 1] == str(wd / "schema.json")
assert f"model_instructions_file='{sysfile.as_posix()}'" in a and json.loads((wd / "schema.json").read_text()) == P.SCHEMA
a = P.cli_argv(P.engine_spec({"prompt_engine": "codex", "prompt_model": "gpt-5-codex"}), sysfile, wd)
assert a[a.index("-m") + 1] == "gpt-5-codex"
a = P.cli_argv(P.engine_spec({"prompt_engine": "gemini", "prompt_model": "gemini-2.5-flash"}), sysfile, wd)
assert a[1:] == ["--output-format", "json", "-e", "none", "-m", "gemini-2.5-flash"]
env = P.cli_env(P.engine_spec({"prompt_engine": "claude"}), sysfile)
assert env["MAX_THINKING_TOKENS"] == "0" and not any(k.startswith("CLAUDE") for k in env)
os.environ["CODEX_API_KEY"] = "x"
env = P.cli_env(P.engine_spec({"prompt_engine": "codex"}), sysfile)
assert "CODEX_API_KEY" not in env and "OPENAI_API_KEY" not in env
del os.environ["CODEX_API_KEY"]
env = P.cli_env(P.engine_spec({"prompt_engine": "gemini"}), sysfile)
assert env["GEMINI_SYSTEM_MD"] == str(sysfile) and env["NO_BROWSER"] == "true"
# an npm .cmd shim is run through node on the script it points at, never through cmd.exe
shim = tmp / "codex.cmd"
shim.write_text('@ECHO off\r\n"%dp0%\\node_modules\\codex\\bin\\codex.js" %*\r\n')
(tmp / "node_modules" / "codex" / "bin").mkdir(parents=True)
(tmp / "node_modules" / "codex" / "bin" / "codex.js").write_text("")
assert P.node_script(str(shim)) == str(tmp / "node_modules" / "codex" / "bin" / "codex.js")
assert P.node_script(str(fake)) is None
P.find_exe = _find
# the shim's native exe wins over the shim; nothing found -> None
native = tmp / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin"
native.mkdir(parents=True)
(native / "codex.exe").write_text("")
_w, _env = P.shutil.which, dict(os.environ)
P.shutil.which = lambda n: str(shim) if n == "codex" else None
assert P.find_exe("codex") == str(native / "codex.exe")
os.environ["APPDATA"] = str(tmp / "nowhere")
P.shutil.which = lambda n: None
assert P.find_exe("nothing-here") is None
os.environ.clear(); os.environ.update(_env)
P.shutil.which = _w
print("argv ok")

# --- the runner against fake CLIs ------------------------------------------------------------
py = sys.executable
envelope = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(good),
            "structured_output": good, "modelUsage": {"claude-haiku-4-5": {}, "claude-sonnet-5": {}}}
fake_claude = tmp / "fakeclaude.py"
fake_claude.write_text(
    "import sys, json\n"
    "msg = sys.stdin.read()\n"
    "assert 'PROMPTIFY-SYSTEM' not in msg and '<dictation>' in msg, msg[:80]\n"
    f"print({json.dumps(json.dumps(envelope))})\n")
work = tmp / "work"
sysf = tmp / "promptify.txt"
sysf.write_text("PROMPTIFY-SYSTEM rules\n", encoding="utf-8")
P.SYSTEM_FILE = sysf
P.log = lambda s: None
real_argv = P.cli_argv


def fake_argv(spec, sysfile, workdir):
    script = tmp / {"claude": "fakeclaude.py", "codex": "fakecodex.py", "gemini": "fakegemini.py"}[spec["key"]]
    return [py, str(script), spec["model"]]


P.cli_argv = fake_argv
res = P.draft({"prompt_engine": "claude"}, "make it faster, right", "code", workdir=work)
assert res["prompts"] == ["Do X"] and res["questions"] == good["questions"] and res["notes"] == "n"
assert res["engine"] == "claude" and res["model"] == "claude-sonnet-5" and res["target"] == "code" and res["wall"] >= 0
assert (work / "promptify" / "promptify_system.txt").read_text(encoding="utf-8") == "PROMPTIFY-SYSTEM rules\n"
assert (work / "promptify" / "empty").is_dir()

fake_codex = tmp / "fakecodex.py"        # a JSONL event stream; the dictation alone on stdin
fake_codex.write_text(
    "import sys, json\n"
    "msg = sys.stdin.read(); assert 'PROMPTIFY-SYSTEM' not in msg and '<dictation>' in msg\n"
    "print(json.dumps({'type': 'thread.started'}))\n"
    "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'thinking...'}}))\n"
    f"print(json.dumps({{'type': 'item.completed', 'item': {{'type': 'agent_message', 'text': '```json\\n' + {json.dumps(json.dumps(good))} + '\\n```'}}}}))\n")
res = P.draft({"prompt_engine": "codex", "prompt_model": "m"}, "d", "chat", workdir=work)
assert res["prompts"] == ["Do X"] and res["engine"] == "codex" and res["model"] == "m"

fake_gemini = tmp / "fakegemini.py"      # {"response": ..., "stats": ...}; system prompt via env
fake_gemini.write_text(
    "import sys, json, os\n"
    "msg = sys.stdin.read(); assert 'PROMPTIFY-SYSTEM' not in msg and os.environ['GEMINI_SYSTEM_MD']\n"
    f"print(json.dumps({{'response': {json.dumps(json.dumps(good))}, 'stats': {{}}}}))\n")
res = P.draft({"prompt_engine": "gemini"}, "d", "code", workdir=work)
assert res["prompts"] == ["Do X"] and res["engine"] == "gemini"

# errors: an auth failure envelope, a crash, and a timeout / cancel that must kill the child
# codex: a usage-limit turn.failed with no message -> the error, not a parse failure
limit = tmp / "limit.py"
limit.write_text("import json\nprint(json.dumps({'type': 'turn.failed', 'error': {'message': \"You've hit your usage limit\"}}))\n"
                 "raise SystemExit(1)", encoding="utf-8")
P.cli_argv = lambda spec, sysfile, workdir: [py, str(limit)]
try:
    P.draft({"prompt_engine": "codex"}, "d", workdir=work)
    raise AssertionError("limit not raised")
except P.PromptifyError as e:
    assert "usage limit" in str(e), e
fail = tmp / "fail.py"
fail.write_text("import json; print(json.dumps({'is_error': True, 'result': 'Not logged in · Please run /login'}))",
                encoding="utf-8")
P.cli_argv = lambda spec, sysfile, workdir: [py, str(fail)]
try:
    P.draft({"prompt_engine": "claude"}, "d", workdir=work)
    raise AssertionError("auth error not raised")
except P.PromptifyError as e:
    assert "not logged in" in str(e) and "`claude`" in str(e), e
crash = tmp / "crash.py"
crash.write_text("import sys; sys.stderr.write('boom\\n'); sys.exit(3)")
P.cli_argv = lambda spec, sysfile, workdir: [py, str(crash)]
try:
    P.draft({"prompt_engine": "claude"}, "d", workdir=work)
    raise AssertionError("crash not raised")
except P.PromptifyError as e:
    assert "boom" in str(e), e
sleeper = tmp / "sleep.py"
sleeper.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(30)\nprint('{}')")
P.cli_argv = lambda spec, sysfile, workdir: [py, str(sleeper)]
P.TIMEOUT = 1.0
t0 = time.monotonic()
try:
    P.draft({"prompt_engine": "claude"}, "d", workdir=work)
    raise AssertionError("timeout not raised")
except P.PromptifyError as e:
    assert "Timed out" in str(e) and time.monotonic() - t0 < 8, (e, time.monotonic() - t0)
P.TIMEOUT = 150
ev = threading.Event()
threading.Timer(0.5, ev.set).start()
t0 = time.monotonic()
try:
    P.draft({"prompt_engine": "claude"}, "d", cancel=ev, workdir=work)
    raise AssertionError("cancel not raised")
except P.Cancelled:
    assert time.monotonic() - t0 < 8, time.monotonic() - t0
P.cli_argv = real_argv
print("runner ok: envelopes, JSONL, auth, crash, timeout, cancel")

# --- API engines: request shape and error mapping, against a fake http_json ------------------
calls = []


def fake_http(url, body, headers, timeout=120):
    calls.append((url, body, headers))
    if "anthropic" in url:
        return {"content": [{"type": "text", "text": json.dumps(good)}]}
    if body.get("model") == "bad":
        raise P.PromptifyError("unknown model or URL: bad")
    return {"choices": [{"message": {"content": json.dumps(good)}}]}


P.http_json = fake_http
res = P.draft({"prompt_engine": "openrouter", "prompt_key": "k"}, "d", "code", workdir=work)
url, body, headers = calls[-1]
assert url == "https://openrouter.ai/api/v1/chat/completions" and headers["Authorization"] == "Bearer k"
assert headers["X-Title"] == "murmur" and body["model"] == "anthropic/claude-sonnet-4.5"
assert body["messages"][0] == {"role": "system", "content": "PROMPTIFY-SYSTEM rules\n"}
assert body["messages"][1]["role"] == "user" and "<dictation>" in body["messages"][1]["content"]
assert body["response_format"] == {"type": "json_object"} and res["prompts"] == ["Do X"]
res = P.draft({"prompt_engine": "anthropic", "prompt_key": "k", "prompt_model": "claude-sonnet-4-5"}, "d", workdir=work)
url, body, headers = calls[-1]
assert url == "https://api.anthropic.com/v1/messages" and headers["x-api-key"] == "k" and body["system"].startswith("PROMPTIFY")
assert body["max_tokens"] == 4096 and res["prompts"] == ["Do X"] and res["model"] == "claude-sonnet-4-5"
res = P.draft({"prompt_engine": "gemini_api", "prompt_key": "k"}, "d", workdir=work)
url, body, headers = calls[-1]
assert url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions" and body["model"] == "gemini-2.5-flash"
res = P.draft({"prompt_engine": "custom", "prompt_url": "http://localhost:11434/v1/", "prompt_model": "qwen"}, "d", workdir=work)
url, body, headers = calls[-1]
assert url == "http://localhost:11434/v1/chat/completions" and "Authorization" not in headers and body["model"] == "qwen"
try:
    P.draft({"prompt_engine": "openai", "prompt_key": "k", "prompt_model": "bad"}, "d", workdir=work)
    raise AssertionError
except P.PromptifyError as e:
    assert "unknown model" in str(e)
try:
    P.draft({"prompt_engine": "custom", "prompt_url": "http://x/v1"}, "d", workdir=work)
    raise AssertionError
except P.PromptifyError as e:
    assert "set a model" in str(e)
print("api engines ok")

# the shipped system prompt is the real one and the schema is what the engines are told to fill
real = (Path(__file__).resolve().parents[1] / "promptify.txt").read_text(encoding="utf-8")
assert "<dictation>" in real and "Fidelity" in real and len(real) > 5000
assert P.SCHEMA["required"] == ["prompt", "questions"] and json.loads(P.SCHEMA_JSON) == P.SCHEMA
print("all promptify tests passed")
