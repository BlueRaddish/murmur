"""connect: connection state from credential files, and the browser sign-in flows driven
against fake CLIs (URL parsing, paste-code fallback, consent answering, cancel, timeout) plus
the OpenRouter PKCE flow with a fake browser and a fake exchange. No real login is touched.
Run: python tests/test_connect.py"""
import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import promptify as P
import connect as C

home = Path(tempfile.mkdtemp())
app = home / "murmur"
paths = C.cred_paths(home, app)
C.HOME = home                       # available() has no home argument: it reads the real one
assert paths["claude"] == home / ".claude" / ".credentials.json" and paths["openrouter"] == app / "openrouter.json"
_find = P.find_exe
P.find_exe = lambda name: r"C:\fake\%s.exe" % name

# --- status from files ---------------------------------------------------------------------
for k in P.ORDER:
    assert C.status(k, home, app) == ("none", "Not connected"), k
paths["claude"].parent.mkdir(parents=True)
now_ms = int(time.time() * 1000)
paths["claude"].write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshToken": "y",
                                                          "expiresAt": now_ms - 1, "refreshTokenExpiresAt": now_ms + 86400000,
                                                          "subscriptionType": "max"}}))
assert C.status("claude", home, app) == ("connected", "Max")        # an expired access token is fine: -p refreshes
paths["claude"].write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshTokenExpiresAt": now_ms - 1000}}))
assert C.status("claude", home, app)[0] == "expired"
paths["claude"].write_text(json.dumps({"claudeAiOauth": {"accessToken": ""}}))
assert C.status("claude", home, app)[0] == "none"
paths["claude"].write_text("not json")
assert C.status("claude", home, app)[0] == "none"
paths["codex"].parent.mkdir(parents=True)
paths["codex"].write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "t", "refresh_token": "r"}}))
assert C.status("codex", home, app) == ("connected", "ChatGPT")
paths["gemini"].parent.mkdir(parents=True)
paths["gemini"].write_text(json.dumps({"access_token": "a", "refresh_token": "r", "expiry_date": 1}))
assert C.status("gemini", home, app) == ("connected", "Google")
paths["gemini"].write_text(json.dumps({"access_token": "a"}))
assert C.status("gemini", home, app)[0] == "none"
paths["openrouter"].parent.mkdir(parents=True)
paths["openrouter"].write_text(json.dumps({"key": "sk-or-v1-abc"}))
assert C.status("openrouter", home, app) == ("connected", "OpenRouter") and C.openrouter_key(app) == "sk-or-v1-abc"
C.forget_openrouter(app)
assert C.status("openrouter", home, app)[0] == "none" and C.openrouter_key(app) == ""
P.find_exe = lambda name: None
assert C.status("claude", home, app)[0] == "missing" and C.status("openrouter", home, app)[0] == "none"
P.find_exe = lambda name: r"C:\fake\%s.exe" % name
assert C.status("nope", home, app)[0] == "none"
# availability follows the connection state, not the presence of a key field
paths["codex"].write_text(json.dumps({"tokens": {}}))
assert P.available({"prompt_engine": "codex"}, appdir=app)[0] is False
assert "Connect" in P.available({"prompt_engine": "codex"}, appdir=app)[1]
paths["codex"].write_text(json.dumps({"tokens": {"access_token": "t"}}))
assert P.available({"prompt_engine": "codex"}, appdir=app) == (True, "")
P.find_exe = lambda name: None
assert "install" in P.available({"prompt_engine": "gemini"}, appdir=app)[1]
P.find_exe = lambda name: r"C:\fake\%s.exe" % name
print("status ok")

# --- the sign-in flows against fake CLIs --------------------------------------------------------
py = sys.executable
scripts = home / "bin"
scripts.mkdir()
claude_login = scripts / "claude_login.py"
claude_login.write_text(r'''
import sys, json, time
sys.stdout.write("Opening browser to sign in\u2026\n")
sys.stdout.write("If the browser didn't open, visit: \x1b]8;;https://claude.com/cai/oauth/authorize?code=true&state=abc\x1b\\https://claude.com/cai/oauth/authorize?code=true&state=abc\x1b]8;;\x1b\\\n")
sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
while True:
    line = sys.stdin.readline()
    if not line: sys.exit(1)
    if line.strip() == "code#abc":
        open(sys.argv[1], "w").write(json.dumps({"claudeAiOauth": {"accessToken": "t", "refreshTokenExpiresAt": 4102444800000, "subscriptionType": "max"}}))
        print("Login successful"); sys.exit(0)
    sys.stderr.write("Invalid code. Please make sure the full code was copied.\n"); sys.stderr.flush()
''', encoding="utf-8")
events = []
_head = P.cli_head
P.cli_head = lambda spec: [py, str(claude_login), str(paths["claude"])]
paths["claude"].unlink()
lg = C.Login("claude", lambda k, t: events.append((k, t)), home=home, appdir=app, timeout=20).start()
for _ in range(100):
    if any(k == "url" for k, _ in events):
        break
    time.sleep(0.05)
assert ("url", "https://claude.com/cai/oauth/authorize?code=true&state=abc") in events, events
lg.submit("wrong")
time.sleep(0.3)
lg.submit("code#abc")
lg.thread.join(10)
assert events[-1] == ("done", "Max"), events
assert C.status("claude", home, app) == ("connected", "Max")

codex_login = scripts / "codex_login.py"
codex_login.write_text(r'''
import sys, json, time
sys.stderr.write("Starting local login server on http://localhost:1455.\nIf your browser did not open, navigate to this URL to authenticate:\n\nhttps://auth.openai.com/oauth/authorize?state=xyz\n\n"); sys.stderr.flush()
time.sleep(0.4)
open(sys.argv[1], "w").write(json.dumps({"tokens": {"access_token": "t"}}))
sys.stderr.write("Successfully logged in\n"); sys.exit(0)
''', encoding="utf-8")
P.cli_head = lambda spec: [py, str(codex_login), str(paths["codex"])]
paths["codex"].unlink()
events = []
lg = C.Login("codex", lambda k, t: events.append((k, t)), home=home, appdir=app, timeout=20).start()
lg.thread.join(10)
assert events[0] == ("url", "https://auth.openai.com/oauth/authorize?state=xyz") and events[-1] == ("done", "ChatGPT"), events

gemini_login = scripts / "gemini_login.py"
gemini_login.write_text(r'''
import sys, json
sys.stdout.write("\nOpening authentication page in your browser. Do you want to continue? [Y/n]: "); sys.stdout.flush()
line = sys.stdin.readline()
if line.strip().lower() != "y":
    sys.stderr.write("Error authenticating: FatalCancellationError: Authentication cancelled by user.\n"); sys.exit(130)
open(sys.argv[1], "w").write(json.dumps({"access_token": "a", "refresh_token": "r"}))
print(json.dumps({"response": "OK", "stats": {}})); sys.exit(0)
''', encoding="utf-8")
P.cli_head = lambda spec: [py, str(gemini_login), str(paths["gemini"])]
paths["gemini"].unlink()
events = []
lg = C.Login("gemini", lambda k, t: events.append((k, t)), home=home, appdir=app, timeout=20).start()
lg.thread.join(10)
assert events == [("done", "Google")], events                  # the consent was answered, no URL is printed

# cancel kills the child; a child that never finishes hits murmur's timeout
sleeper = scripts / "sleep_login.py"
sleeper.write_text("import sys, time\nsys.stderr.write('https://example.com/auth\\n'); sys.stderr.flush()\ntime.sleep(60)\n")
P.cli_head = lambda spec: [py, str(sleeper)]
events = []
lg = C.Login("codex", lambda k, t: events.append((k, t)), home=home, appdir=app, timeout=20).start()
time.sleep(0.8)
t0 = time.monotonic()
lg.cancel()
lg.thread.join(10)
assert events[-1] == ("cancelled", "") and time.monotonic() - t0 < 6, events
events = []
lg = C.Login("codex", lambda k, t: events.append((k, t)), home=home, appdir=app, timeout=1.0).start()
lg.thread.join(10)
assert events[-1][0] == "error" and "try Connect again" in events[-1][1], events
failing = scripts / "fail_login.py"
failing.write_text("import sys\nsys.stderr.write('Error logging in: Port 127.0.0.1:1457 is already in use\\n'); sys.exit(1)\n")
P.cli_head = lambda spec: [py, str(failing)]
events = []
C.Login("codex", lambda k, t: events.append((k, t)), home=home, appdir=app).start().thread.join(10)
assert events == [("error", "Error logging in: Port 127.0.0.1:1457 is already in use")], events
P.cli_head = _head
print("cli logins ok: url, paste code, consent, cancel, timeout, failure")

# --- OpenRouter PKCE with a fake browser (it hits the callback itself) and a fake exchange ---
def fake_browser(url):
    q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    cb = urllib.request.unquote(q["callback_url"])
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    threading.Timer(0.2, lambda: urllib.request.urlopen(cb + "?code=thecode").read()).start()
    return True


seen = {}


def fake_exchange(code, verifier, timeout=20.0):
    seen.update(code=code, verifier=verifier)
    return "sk-or-v1-newkey"


_ex = C.OpenRouterConnect.exchange
C.OpenRouterConnect.exchange = staticmethod(fake_exchange)
events = []
lg = C.Login("openrouter", lambda k, t: events.append((k, t)), home=home, appdir=app, timeout=20, open_browser=fake_browser).start()
lg.thread.join(10)
assert events[0][0] == "url" and events[0][1].startswith("https://openrouter.ai/auth?callback_url=http%3A%2F%2Flocalhost%3A") and events[-1] == ("done", "OpenRouter"), events
assert seen["code"] == "thecode" and len(seen["verifier"]) == 43
assert C.status("openrouter", home, app)[0] == "connected" and C.openrouter_key(app) == "sk-or-v1-newkey"
# a wrong path on the callback server is a 404 and captures nothing; cancel unblocks wait()
oc = C.OpenRouterConnect(timeout=5, open_browser=lambda u: True)
url = oc.start()
try:
    urllib.request.urlopen(f"http://127.0.0.1:{oc.port}/nope?code=x")
    raise AssertionError
except urllib.error.HTTPError as e:
    assert e.code == 404
threading.Timer(0.2, oc.cancel).start()
try:
    oc.wait()
    raise AssertionError
except C.OpenRouterConnect.Error as e:
    assert str(e) == "cancelled"
C.OpenRouterConnect.exchange = _ex
# exec: the engine reads murmur's own key; without it the message points at Connect
calls = []
P.http_json = lambda url, body, headers, timeout=120: (calls.append((url, body, headers)) or {"choices": [{"message": {"content": json.dumps({"prompt": "P", "questions": []})}}]})
P.log = lambda s: None
res = P.draft({"prompt_engine": "openrouter"}, "d", "code", workdir=app)
assert res["prompts"] == ["P"] and calls[-1][2]["Authorization"] == "Bearer sk-or-v1-newkey" and calls[-1][1]["model"] == "openai/gpt-5.6-luna"
C.forget_openrouter(app)
try:
    P.draft({"prompt_engine": "openrouter"}, "d", "code", workdir=app)
    raise AssertionError
except P.PromptifyError as e:
    assert "Connect" in str(e)
P.find_exe = _find
print("openrouter ok")
print("all connect tests passed")
