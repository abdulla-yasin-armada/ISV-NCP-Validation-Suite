#!/usr/bin/env python3
"""BridgeClient — HTTP client for the Armada Bridge auth-gateway API.

Auth: POST /auth/login with {email, password} sets a session cookie (sid).
All subsequent requests carry that cookie automatically — same as the browser UI.
No Keycloak token endpoint or client credentials needed.

Cookie persistence: from_env() always performs a fresh login (deletes any stale cookie).
from_env_users() reuses a cached session cookie and only re-authenticates when absent or expired.

MFA: set BRIDGE_TOTP_SECRET to the base32 TOTP secret — the current 6-digit code is
generated automatically at login time (RFC 6238, same algorithm as Google Authenticator).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import http.cookiejar
import json
import os
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Union

from .file_logger import FileLogger, get_file_logger

# JSON payloads from Bridge APIs may be objects or arrays (e.g. GET /users).
JsonValue = Union[dict[str, Any], list[Any]]

_log: FileLogger = get_file_logger(__name__)


def _totp(secret_b32: str, *, digits: int = 6, interval: int = 30) -> str:
    """Generate the current TOTP code from a base32-encoded secret (RFC 6238)."""
    key = base64.b32decode(secret_b32.upper().replace(" ", ""))
    counter = int(time.time()) // interval
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)

_DEFAULT_COOKIE_PATH: Path = Path.home() / ".cache" / "isvctl" / "bridge_session.cookies"

_ENV_HINTS: dict[str, str] = {
    "BRIDGE_URL":      "Bridge API endpoint  e.g. export BRIDGE_URL=https://bridge.example.com",
    "BRIDGE_USERNAME": "Bridge login email   e.g. export BRIDGE_USERNAME=you@example.com",
    "BRIDGE_PASSWORD": "Bridge password      e.g. export BRIDGE_PASSWORD=yourpassword",
}


def _require_env(*names: str) -> None:
    """Raise a clear RuntimeError listing every missing required env var."""
    missing = [n for n in names if not os.environ.get(n)]
    if not missing:
        return
    lines = ["The following required environment variables are not set:\n"]
    for name in missing:
        hint = _ENV_HINTS.get(name, f"export {name}=<value>")
        lines.append(f"  {name:20s}  {hint}")
    lines.append(
        "\nSet them before running the suite:\n"
        "  export BRIDGE_URL=...\n"
        "  export BRIDGE_USERNAME=...\n"
        "  export BRIDGE_PASSWORD=...\n"
        "  export BRIDGE_TENANT=<your-tenant-name-or-uuid>\n"
        "\nIf your Bridge instance uses a self-signed certificate:\n"
        "  export BRIDGE_INSECURE=1"
    )
    raise RuntimeError("\n".join(lines))

_SENSITIVE_KEYS: frozenset[str] = frozenset({"password", "token", "secret", "totp", "apiKey", "api_key"})


def _redact(body: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of body with sensitive field values replaced by '***'."""
    return {k: "***" if k in _SENSITIVE_KEYS else v for k, v in body.items()}


class BridgeClient:
    """HTTP client for the Bridge auth-gateway API.

    Authenticates via POST /auth/login — receives a session cookie (sid) and
    replays it on every subsequent request, exactly as the browser UI does.
    from_env() always performs a fresh login. from_env_users() persists the
    session cookie to disk and reuses it when valid.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        totp_secret: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        cookie_path: Path | None = _DEFAULT_COOKIE_PATH,
        host_header: str | None = None,
    ) -> None:
        """Initialise client fields; does NOT authenticate. Call login() or use from_env()."""
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._totp_secret = totp_secret
        self._ssl_context = ssl_context
        self._cookie_path = cookie_path
        self._host_header = host_header

        if cookie_path is not None:
            jar: http.cookiejar.MozillaCookieJar = http.cookiejar.MozillaCookieJar(str(cookie_path))
            if cookie_path.exists():
                try:
                    jar.load(ignore_discard=True, ignore_expires=True)
                except (OSError, http.cookiejar.LoadError):
                    pass
        else:
            jar = http.cookiejar.MozillaCookieJar()

        self._jar = jar
        handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPCookieProcessor(jar)]
        if ssl_context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
        self._opener = urllib.request.build_opener(*handlers)
        self._opener.addheaders = [("User-Agent", "curl/8.7.1")]

    @classmethod
    def from_env(cls) -> BridgeClient:
        """Construct a client from environment variables, always performing a fresh login.

        Reads: BRIDGE_URL, BRIDGE_USERNAME (email), BRIDGE_PASSWORD.
        Optional: BRIDGE_TOTP_SECRET — base32 TOTP secret; when set, the current
        6-digit code is generated at login time (MFA-enabled accounts).
        Optional: BRIDGE_INSECURE=1 — disable TLS certificate verification (dev/self-signed certs).
        Optional: BRIDGE_HOST — HTTP Host header when BRIDGE_URL is an IP:port (lab ingress).
        Deletes any stale cached session cookie before logging in to guarantee a fresh session.
        """
        _require_env("BRIDGE_URL", "BRIDGE_USERNAME", "BRIDGE_PASSWORD")
        if _DEFAULT_COOKIE_PATH.exists():
            _DEFAULT_COOKIE_PATH.unlink()
        ssl_context: ssl.SSLContext | None = None
        if os.environ.get("BRIDGE_INSECURE") == "1":
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        host_header = os.environ.get("BRIDGE_HOST", "").strip() or None
        client = cls(
            base_url=os.environ["BRIDGE_URL"],
            username=os.environ["BRIDGE_USERNAME"],
            password=os.environ["BRIDGE_PASSWORD"],
            totp_secret=os.environ.get("BRIDGE_TOTP_SECRET"),
            ssl_context=ssl_context,
            host_header=host_header,
        )
        client.login()
        return client

    @classmethod
    def from_env_users(cls) -> BridgeClient:
        """Construct a client targeting the Bridge users service.

        The /users/* endpoints (create, list, delete) are served by a separate
        service that is only reachable via the public hostname (BRIDGE_HOST), not
        via the internal IP:port used by BRIDGE_URL for orchestrator APIs.

        When BRIDGE_HOST is set, uses https://{BRIDGE_HOST} as the base URL.
        Falls back to BRIDGE_URL when BRIDGE_HOST is not set.
        Uses a separate cookie cache to avoid collisions with from_env().
        TLS verification is still controlled by BRIDGE_INSECURE.
        """
        _require_env("BRIDGE_URL", "BRIDGE_USERNAME", "BRIDGE_PASSWORD")
        ssl_context: ssl.SSLContext | None = None
        if os.environ.get("BRIDGE_INSECURE") == "1":
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        host_header = os.environ.get("BRIDGE_HOST", "").strip() or None
        base_url = (
            f"https://{host_header}"
            if host_header
            else os.environ["BRIDGE_URL"]
        )
        cookie_path = Path(f"/tmp/.bridge_users_cookie_{os.getpid()}.txt")
        client = cls(
            base_url=base_url,
            username=os.environ["BRIDGE_USERNAME"],
            password=os.environ["BRIDGE_PASSWORD"],
            totp_secret=os.environ.get("BRIDGE_TOTP_SECRET"),
            ssl_context=ssl_context,
            host_header=None,
            cookie_path=cookie_path,
        )
        if not client._session_valid():
            client.login()
        return client

    def _session_valid(self) -> bool:
        """Return True if a non-expired sid cookie is present in the jar."""
        now = time.time()
        for cookie in self._jar:
            if cookie.name == "sid":
                # Session cookies have no explicit expiry — trust them until the server rejects.
                return cookie.expires is None or cookie.expires > now
        return False

    def _save_cookies(self) -> None:
        if self._cookie_path is None:
            return
        self._cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self._jar.save(ignore_discard=True, ignore_expires=True)

    def _with_host(self, req: urllib.request.Request) -> urllib.request.Request:
        """Attach ingress Host header when BRIDGE_URL is IP:port (lab internal access)."""
        if self._host_header:
            req.add_header("Host", self._host_header)
        return req

    def login(self) -> None:
        """Authenticate via POST /auth/login and persist the session cookie to disk.

        POST {base_url}/auth/login
        Content-Type: application/json
        Body: {email: BRIDGE_USERNAME, password: BRIDGE_PASSWORD}
        The auth-gateway sets a session cookie (sid) which the opener stores
        and replays automatically on all subsequent requests.
        """
        body: dict[str, str] = {"email": self._username, "password": self._password}
        if self._totp_secret:
            body["totp"] = _totp(self._totp_secret)
        data = json.dumps(body).encode()
        req = self._with_host(
            urllib.request.Request(
                self.base_url + "/auth/login",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
        _log.debug("POST %s/auth/login (credentials redacted)", self.base_url)
        for attempt in range(2):
            try:
                with self._opener.open(req, timeout=30) as resp:
                    _log.debug("POST /auth/login -> %d", resp.status)
                self._save_cookies()
                return
            except (http.client.RemoteDisconnected, ConnectionResetError, TimeoutError) as e:
                if attempt == 0:
                    _log.debug("POST /auth/login — transient error, retrying (%s)", e)
                    continue
                raise

    def login_as(self, email: str, password: str) -> BridgeClient:
        """Return a new BridgeClient authenticated as a different user.

        Used in IAM create_user flow: POST /key-manager/api-key must be called
        by the target user (not the admin). Creates a fresh, ephemeral session
        (not persisted to disk) for the given credentials.
        """
        client = BridgeClient(
            base_url=self.base_url,
            username=email,
            password=password,
            ssl_context=self._ssl_context,
            cookie_path=None,
            host_header=self._host_header,
        )
        client.login()
        return client

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> JsonValue:
        """GET {self.base_url}{path} with session cookie auth."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        _log.debug("GET %s", url)
        req = self._with_host(urllib.request.Request(url, method="GET"))
        req.add_header("Accept", "application/json")
        for attempt in range(2):
            try:
                with self._opener.open(req, timeout=30) as resp:
                    raw = resp.read().decode()
                    _log.debug("GET %s -> %d  body=%s", url, resp.status, raw)
                break
            except (http.client.RemoteDisconnected, ConnectionResetError, TimeoutError) as e:
                if attempt == 0:
                    _log.debug("GET %s — transient error, retrying (%s)", url, e)
                    continue
                raise
            except urllib.error.HTTPError as e:
                raw = e.read().decode()
                _log.debug("GET %s -> %d  body=%s", url, e.code, raw)
                raise ValueError(f"GET {path} failed with status {e.code}: {raw}") from e
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            _log.warning("GET %s returned non-JSON response (%d bytes): %.200r", path, len(raw), raw)
            return {}

    def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """POST {self.base_url}{path} with session cookie auth."""
        payload = body if body is not None else {}
        data = json.dumps(payload).encode()
        req = self._with_host(
            urllib.request.Request(
                self.base_url + path,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )
        _log.debug("POST %s%s  body=%s", self.base_url, path, json.dumps(_redact(payload)))
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                _log.debug("POST %s%s -> %d  body=%s", self.base_url, path, resp.status, raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            _log.debug("POST %s%s -> %d  body=%s", self.base_url, path, e.code, raw)
            raise ValueError(f"POST {path} failed with status {e.code}: {raw}") from e
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            _log.warning(
                "POST %s returned non-JSON response (%d bytes): %.200r",
                path,
                len(raw),
                raw,
            )
            return {}

    def post_multipart(
        self,
        path: str,
        fields: dict[str, str | list[str]],
        *,
        timeout: int = 120,
    ) -> dict[str, Any]:
        """POST multipart/form-data to {self.base_url}{path} with session cookie auth."""
        boundary = f"----isvctl{hashlib.sha256(os.urandom(16)).hexdigest()[:24]}"
        parts: list[bytes] = []
        for name, value in fields.items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                parts.append(f"--{boundary}\r\n".encode())
                parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
                parts.append(item.encode())
                parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        req = self._with_host(
            urllib.request.Request(
                self.base_url + path,
                data=data,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
        )
        _log.debug("POST %s%s  multipart fields=%s", self.base_url, path, list(fields.keys()))
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                _log.debug("POST %s%s -> %d  body=%s", self.base_url, path, resp.status, raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            _log.debug("POST %s%s -> %d  body=%s", self.base_url, path, e.code, raw)
            raise ValueError(f"POST {path} failed with status {e.code}: {raw}") from e
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            _log.warning("POST %s returned non-JSON response (%d bytes): %.200r", path, len(raw), raw)
            return {}
        if isinstance(parsed, list):
            return parsed[0] if parsed else {}
        return parsed

    def delete(self, path: str) -> dict[str, Any]:
        """DELETE {self.base_url}{path} with session cookie auth."""
        _log.debug("DELETE %s%s", self.base_url, path)
        req = self._with_host(urllib.request.Request(self.base_url + path, method="DELETE"))
        try:
            with self._opener.open(req, timeout=30) as resp:
                raw = resp.read().decode()
                _log.debug("DELETE %s%s -> %d  body=%s", self.base_url, path, resp.status, raw)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Already deleted — treat as success without parsing the error body.
                _log.debug("DELETE %s%s -> 404 (already deleted)", self.base_url, path)
                return {}
            raw = e.read().decode()
            _log.debug("DELETE %s%s -> %d  body=%s", self.base_url, path, e.code, raw)
            raise ValueError(f"DELETE {path} failed with status {e.code}: {raw}") from e
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # DELETE succeeded but response body is not JSON (e.g. "OK", plain text).
            return {}

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """PATCH {self.base_url}{path} with session cookie auth."""
        data = json.dumps(body).encode()
        req = self._with_host(
            urllib.request.Request(
                self.base_url + path,
                data=data,
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
        )
        _log.debug("PATCH %s%s  body=%s", self.base_url, path, json.dumps(_redact(body)))
        try:
            with self._opener.open(req, timeout=30) as resp:
                raw = resp.read().decode()
                _log.debug("PATCH %s%s -> %d  body=%s", self.base_url, path, resp.status, raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            _log.debug("PATCH %s%s -> %d  body=%s", self.base_url, path, e.code, raw)
            raise ValueError(f"PATCH {path} failed with status {e.code}: {raw}") from e
        return json.loads(raw) if raw else {}

    def wait_for_state(
        self,
        path: str,
        target_state: str,
        *,
        state_field: str = "state",
        timeout: int = 300,
        interval: int = 10,
    ) -> dict[str, Any]:
        """Poll GET path until response[state_field] == target_state or timeout.

        Returns the final response dict.
        Raises TimeoutError if target_state is not reached within timeout seconds.
        """
        deadline = time.monotonic() + timeout
        while True:
            resp = self.get(path)
            if resp.get(state_field) == target_state:
                return resp
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out after {timeout}s waiting for {path} "
                    f"{state_field!r} == {target_state!r}; last value: {resp.get(state_field)!r}"
                )
            time.sleep(interval)
