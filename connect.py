"""Connect: the engines' browser sign-in and their connection state, for Promptify.

Engines are login-only (the user's ruling: "linked through login on web, not directly taking
[keys]"). Three of them are CLIs that hold their own login - murmur shells out to the binary the
user signed into and never touches the tokens: Anthropic permits signing in to the unmodified
claude.exe with one's own subscription but not routing its credentials elsewhere, and OpenAI and
Google offer no third-party web login at all. OpenRouter is the one provider with first-party
OAuth for desktop apps (PKCE); the key it hands back is kept in murmur's own file.

What this module reads is *presence*: whether a credential file exists and is not past its own
refresh expiry. Values are never read out, logged or shown. Disconnect for a CLI means "stop
using it" - murmur never runs a logout or deletes a credential file that is not its own.

Everything about the flows was measured on 2026-08-29 (NOTES.md): claude prints its URL on
stdout wrapped in OSC-8 escapes and opens the browser itself; codex prints on stderr and cannot
be kept from opening the browser; gemini asks "[Y/n]:" on stdout and hangs forever if stdin is
closed before it does; none of them times out on their own, so the timeout is murmur's.
"""
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import promptify as P

TIMEOUT = 300                    # s to wait for the browser: a sign-in is a minute, not five
URL_RE = re.compile(r"https://[^\s\x1b'\"<>]+")
OSC8 = re.compile(r"\x1b\]8;;[^\x1b]*\x1b\\")
CONSENT = "[Y/n]:"               # gemini's "Opening authentication page in your browser..." prompt
SCRUB = {                        # env that would outrank the login the user made
    "claude": lambda n: n.startswith("CLAUDE") or n in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "codex": lambda n: n in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"),
    "gemini": lambda n: False,
    "openrouter": lambda n: False,
}
STATES = {"connected": "Connected", "none": "Not connected", "missing": "Not installed",
          "expired": "Login expired"}


HOME = None                      # tests point this at a scratch home; None = the real one


def cred_paths(home=None, appdir=None) -> dict:
    home = Path(home or HOME or Path.home())
    appdir = Path(appdir or Path(os.environ.get("APPDATA", home)) / "murmur")
    return {"claude": home / ".claude" / ".credentials.json",
            "codex": home / ".codex" / "auth.json",
            "gemini": home / ".gemini" / "oauth_creds.json",
            "openrouter": appdir / "openrouter.json"}


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None


def status(key: str, home=None, appdir=None) -> tuple:
    """(state, detail) from the credential files alone - no process, no network. States:
    connected · none · missing (CLI absent) · expired (claude's refresh token past its date).
    The detail is a word for the row ("Max", "ChatGPT", "Google", "OpenRouter"), never an
    email or a token."""
    spec = P.ENGINES.get(key)
    if spec is None:
        return "none", STATES["none"]
    if spec["kind"] == "cli" and not P.find_exe(spec["exe"]):
        return "missing", f"{spec['label']} is not installed"
    j = _load(cred_paths(home, appdir)[key])
    if not isinstance(j, dict):
        return "none", STATES["none"]
    if key == "claude":
        o = j.get("claudeAiOauth") or {}
        if not o.get("accessToken"):
            return "none", STATES["none"]
        exp = o.get("refreshTokenExpiresAt") or 0
        if exp and exp / 1000 < time.time():
            return "expired", STATES["expired"]
        return "connected", (o.get("subscriptionType") or "claude.ai").capitalize()
    if key == "codex":
        return ("connected", "ChatGPT") if (j.get("tokens") or j.get("OPENAI_API_KEY")) else ("none", STATES["none"])
    if key == "gemini":
        return ("connected", "Google") if j.get("refresh_token") else ("none", STATES["none"])
    if key == "openrouter":
        k = j.get("key")
        return ("connected", "OpenRouter") if isinstance(k, str) and k.startswith("sk-or-") else ("none", STATES["none"])
    return "none", STATES["none"]


def openrouter_key(appdir=None) -> str:
    j = _load(cred_paths(appdir=appdir)["openrouter"])
    k = (j or {}).get("key") if isinstance(j, dict) else None
    return k if isinstance(k, str) else ""


def forget_openrouter(appdir=None) -> None:
    """Disconnect = delete murmur's own key file. The key itself stays valid on openrouter.ai
    until the user deletes it there."""
    try:
        cred_paths(appdir=appdir)["openrouter"].unlink()
    except OSError:
        pass


class Cancelled(Exception):
    pass


class Login:
    """One Connect attempt, on its own thread. `on_event(kind, text)` gets, in order: ("url", u)
    once the CLI printed its sign-in link (for a Copy-link fallback), then one of ("done",
    detail) / ("cancelled", "") / ("error", message). cancel() and submit(code) may be called
    from any thread; submit() is claude's paste-the-code fallback."""

    def __init__(self, key, on_event, home=None, appdir=None, timeout=TIMEOUT, open_browser=webbrowser.open):
        self.key, self.on_event, self.home, self.appdir, self.timeout = key, on_event, home, appdir, timeout
        self._open = open_browser
        self._cancel = threading.Event()
        self._proc = None
        self._or = None
        self.thread = None

    def start(self) -> "Login":
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def cancel(self) -> None:
        self._cancel.set()
        if self._or is not None:
            self._or.cancel()

    def submit(self, code: str) -> None:
        p = self._proc
        if p is not None and p.stdin and p.poll() is None:
            try:
                p.stdin.write((code.strip() + "\n").encode("utf-8"))
                p.stdin.flush()
            except OSError:
                pass

    def _run(self) -> None:
        try:
            fn = {"claude": self._claude, "codex": self._codex, "gemini": self._gemini,
                  "openrouter": self._openrouter}[self.key]
            detail = fn()
        except Cancelled:
            self.on_event("cancelled", "")
        except Exception as e:
            self.on_event("error", str(e)[:300])
        else:
            self.on_event("done", detail)

    # --- the CLIs ----------------------------------------------------------------------------
    def _cli(self, argv, env, url_on, answer=None) -> tuple:
        """Run a login command with pipes and no window; report the first https URL seen on
        `url_on` ("out"/"err"); answer a prompt when its text appears; kill on cancel/timeout.
        Returns (rc, stdout, stderr) decoded."""
        kw = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                  cwd=str(Path.home()))
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        p = self._proc = subprocess.Popen(argv, **kw)
        bufs, seen = {"out": bytearray(), "err": bytearray()}, {"url": False, "answered": False}

        def read(stream, name):
            while True:
                chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(1)
                if not chunk:
                    return
                bufs[name] += chunk
                text = bufs[name].decode("utf-8", "replace")
                if name == url_on and not seen["url"]:
                    m = URL_RE.search(OSC8.sub("", text))
                    if m:
                        seen["url"] = True
                        self.on_event("url", m.group(0))
                if answer and not seen["answered"] and answer[0] in text:
                    seen["answered"] = True
                    try:
                        p.stdin.write(answer[1])
                        p.stdin.flush()
                    except OSError:
                        pass
        ts = [threading.Thread(target=read, args=(p.stdout, "out"), daemon=True),
              threading.Thread(target=read, args=(p.stderr, "err"), daemon=True)]
        for t in ts:
            t.start()
        t0 = time.monotonic()
        while p.poll() is None:
            if self._cancel.is_set():
                P.kill_tree(p)
                raise Cancelled()
            if time.monotonic() - t0 > self.timeout:
                P.kill_tree(p)
                raise RuntimeError("No sign-in arrived from the browser - try Connect again.")
            time.sleep(0.2)
        for t in ts:
            t.join(2)
        try:
            p.stdin.close()
        except OSError:
            pass
        return p.returncode, bufs["out"].decode("utf-8", "replace"), bufs["err"].decode("utf-8", "replace")

    def _env(self) -> dict:
        drop = SCRUB[self.key]
        env = {n: v for n, v in os.environ.items() if not drop(n)}
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _connected(self) -> str:
        state, detail = status(self.key, self.home, self.appdir)
        if state != "connected":
            raise RuntimeError("The sign-in did not complete - try Connect again.")
        return detail

    def _claude(self) -> str:
        head = P.cli_head(P.ENGINES["claude"])
        rc, out, err = self._cli(head + ["auth", "login"], self._env(), "out")
        if rc != 0:
            raise RuntimeError(_first_line(err) or _first_line(out) or "Sign-in failed.")
        return self._connected()

    def _codex(self) -> str:
        head = P.cli_head(P.ENGINES["codex"])
        rc, out, err = self._cli(head + ["login"], self._env(), "err")
        if rc != 0:
            raise RuntimeError(_first_line(err) or "Sign-in failed.")
        return self._connected()

    def _gemini(self) -> str:
        head = P.cli_head(P.ENGINES["gemini"])
        env = self._env()
        env.update({"GOOGLE_GENAI_USE_GCA": "true", "GEMINI_CLI_NO_RELAUNCH": "1", "NO_COLOR": "1"})
        rc, out, err = self._cli(head + ["-p", "Reply with OK", "--skip-trust", "-o", "json", "-e", "none"],
                                 env, "out", answer=(CONSENT, b"y\n"))
        if "access_denied" in err or "cancelled" in err.lower():
            raise RuntimeError("Sign-in was declined in the browser.")
        # the exit code is not a contract here (the CLI can crash on its way out): the file is
        return self._connected()

    # --- OpenRouter: OAuth PKCE, in-process ------------------------------------------------------
    def _openrouter(self) -> str:
        self._or = OpenRouterConnect(timeout=self.timeout, open_browser=self._open)
        self.on_event("url", self._or.start())
        try:
            key = self._or.wait()
        except OpenRouterConnect.Error as e:
            if str(e) == "cancelled":
                raise Cancelled()
            raise RuntimeError(str(e))
        p = cred_paths(appdir=self.appdir)["openrouter"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"key": key, "connected_at": time.time()}), encoding="utf-8")
        return "OpenRouter"


def _first_line(s: str) -> str:
    for line in s.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class OpenRouterConnect:
    """https://openrouter.ai/auth with a PKCE challenge and a loopback callback; the code is
    exchanged for a normal user API key (docs: openrouter.ai/docs/use-cases/oauth-pkce).
    Never fetch the auth URL from Python - Cloudflare answers 403 to anything but a browser."""
    AUTH_URL = "https://openrouter.ai/auth"
    EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"
    KEY_URL = "https://openrouter.ai/api/v1/key"
    PATH = "/callback"
    DONE = (b"<!doctype html><meta charset=utf-8><title>murmur</title><body style='font:16px system-ui;"
            b"padding:3em;text-align:center'><h2>murmur is connected to OpenRouter</h2><p>You can close this tab.</p>")
    FAIL = (b"<!doctype html><meta charset=utf-8><title>murmur</title><body style='font:16px system-ui;"
            b"padding:3em;text-align:center'><h2>No code received</h2><p>Go back to murmur and press Connect again.</p>")

    class Error(Exception):
        pass

    def __init__(self, timeout=TIMEOUT, open_browser=webbrowser.open):
        self.timeout, self._open = timeout, open_browser
        self.verifier = _b64url(secrets.token_bytes(32))
        self.challenge = _b64url(hashlib.sha256(self.verifier.encode()).digest())
        self._code, self._got, self._cancelled, self._server = None, threading.Event(), False, None
        self.port = self.auth_url = None

    def _handler(self):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):      # a --noconsole build has no stderr
                pass

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                code = urllib.parse.parse_qs(u.query).get("code", [None])[0]
                ok = u.path == outer.PATH and bool(code)
                body = outer.DONE if ok else outer.FAIL
                self.send_response(200 if ok else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                if ok and outer._code is None:
                    outer._code = code
                    outer._got.set()
        return H

    def start(self) -> str:
        self._server = http.server.HTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]
        self.auth_url = self.AUTH_URL + "?" + urllib.parse.urlencode({
            "callback_url": f"http://localhost:{self.port}{self.PATH}",
            "code_challenge": self.challenge, "code_challenge_method": "S256"})
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True).start()
        if not self._open(self.auth_url):
            self._stop()
            raise self.Error("No browser could be opened.")
        return self.auth_url

    def cancel(self) -> None:
        self._cancelled = True
        self._got.set()

    def _stop(self) -> None:
        s, self._server = self._server, None
        if s is not None:
            s.shutdown()
            s.server_close()

    def wait(self) -> str:
        try:
            got = self._got.wait(self.timeout)
        finally:
            self._stop()
        if self._cancelled:
            raise self.Error("cancelled")
        if not got:
            raise self.Error("No sign-in arrived from the browser - try Connect again.")
        return self.exchange(self._code, self.verifier)

    @classmethod
    def exchange(cls, code: str, verifier: str, timeout=20.0) -> str:
        body = json.dumps({"code": code, "code_verifier": verifier, "code_challenge_method": "S256"}).encode()
        req = urllib.request.Request(cls.EXCHANGE_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "murmur"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise cls.Error(cls._errmsg(e)) from None
        except (urllib.error.URLError, OSError) as e:
            raise cls.Error(f"No connection: {getattr(e, 'reason', e)}") from None
        key = data.get("key") if isinstance(data, dict) else None
        if not isinstance(key, str) or not key.startswith("sk-or-"):
            raise cls.Error("OpenRouter answered without a key.")
        return key

    @classmethod
    def key_status(cls, key: str, timeout=10.0) -> dict:
        """Online: GET /api/v1/key -> {label, limit, limit_remaining, usage, is_free_tier...};
        a 401 means the user deleted the key on openrouter.ai."""
        req = urllib.request.Request(cls.KEY_URL, headers={"Authorization": f"Bearer {key}", "User-Agent": "murmur"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)["data"]
        except urllib.error.HTTPError as e:
            raise cls.Error(cls._errmsg(e)) from None
        except (urllib.error.URLError, OSError) as e:
            raise cls.Error(f"No connection: {getattr(e, 'reason', e)}") from None

    @staticmethod
    def _errmsg(e: urllib.error.HTTPError) -> str:
        try:
            err = json.loads(e.read()).get("error", {})
            return f"{e.code} {err.get('message') or err}"
        except Exception:
            return f"HTTP {e.code}"
