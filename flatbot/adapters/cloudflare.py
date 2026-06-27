from __future__ import annotations

"""
FlareSolverrSession — shared Cloudflare bypass transport.

Both Flatfox and Homegate are protected by Cloudflare Managed Challenge.  This
module provides a single session object that:

  1. Warms up a CF-cleared cookie set for a given base URL by sending the URL
     through FlareSolverr's patched browser (``warmup``).
  2. Reuses those cookies for plain httpx requests (``get_json`` / ``get_html``).
  3. On a 403 or CF interstitial, re-warms once and retries.
  4. Falls back to a full FlareSolverr render (``fetch_via_flaresolverr``) when
     cookie reuse is insufficient.

FlareSolverr must be running locally (default: http://localhost:8191).  If it is
unreachable, ``FlareSolverrError`` is raised with a human-readable hint.
"""

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class FlareSolverrError(RuntimeError):
    """Raised when FlareSolverr is unreachable or returns a non-ok status."""


class FlareSolverrSession:
    """
    Shared transport layer that clears Cloudflare Managed Challenge via a local
    FlareSolverr instance, then reuses the solved cookies for subsequent httpx
    requests.

    One instance is shared across all adapters so each platform is warmed up at
    most once per bot cycle.
    """

    def __init__(
        self,
        flaresolverr_url: str = "http://localhost:8191/v1",
        max_timeout_ms: int = 60_000,
    ) -> None:
        self._fs_url = flaresolverr_url
        self._max_timeout_ms = max_timeout_ms
        self._user_agent = _DEFAULT_UA
        self._client = httpx.Client(
            headers={
                "User-Agent": self._user_agent,
                "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
            },
            timeout=90,
            follow_redirects=True,
        )
        # Tracks which base URLs have a valid CF clearance cookie in _client.
        self._warmed: set[str] = set()

    # ── private helpers ──────────────────────────────────────────────────────

    def _call_flaresolverr(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST *payload* to FlareSolverr and return the parsed response dict."""
        # FlareSolverr itself can take up to max_timeout_ms to solve; add buffer.
        timeout_s = self._max_timeout_ms / 1000 + 30
        try:
            resp = httpx.post(self._fs_url, json=payload, timeout=timeout_s)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        except httpx.HTTPError as exc:
            raise FlareSolverrError(
                f"Cannot reach FlareSolverr at {self._fs_url}: {exc}\n"
                "Make sure it is running:  docker compose up -d flaresolverr"
            ) from exc

        if data.get("status") != "ok":
            msg = data.get("message", "(no message)")
            raise FlareSolverrError(
                f"FlareSolverr returned status={data.get('status')!r}: {msg}"
            )
        return data

    def _inject_cookies(self, cookies: list[dict[str, Any]], ua: str) -> None:
        """Apply FlareSolverr solution cookies + UA into the shared httpx client."""
        self._user_agent = ua
        self._client.headers["User-Agent"] = ua
        for ck in cookies:
            name = ck.get("name", "")
            value = ck.get("value", "")
            # Cloudflare cookie domains arrive as ".flatfox.ch"; httpx wants no dot.
            domain = ck.get("domain", "").lstrip(".")
            if name and domain:
                self._client.cookies.set(name, value, domain=domain)

    def _is_cf_wall(self, resp: httpx.Response) -> bool:
        """Return True if the response looks like a CF challenge interstitial."""
        if resp.status_code == 403:
            return True
        if resp.status_code == 200:
            snippet = resp.text[:3000].lower()
            return "just a moment" in snippet or "checking your browser" in snippet
        return False

    # ── public API ───────────────────────────────────────────────────────────

    def warmup(self, base_url: str) -> None:
        """
        Solve the CF challenge for *base_url* via FlareSolverr's browser.
        Captured ``cf_clearance`` cookie and User-Agent are injected into the
        shared httpx client for reuse.
        """
        log.info("action=flaresolverr_warmup url=%s", base_url)
        payload = {
            "cmd": "request.get",
            "url": base_url,
            "maxTimeout": self._max_timeout_ms,
        }
        data = self._call_flaresolverr(payload)
        solution = data["solution"]
        self._inject_cookies(
            solution.get("cookies", []),
            solution.get("userAgent", self._user_agent),
        )
        self._warmed.add(base_url)
        log.info(
            "action=flaresolverr_warmed url=%s cookies=%d ua=%r",
            base_url,
            len(solution.get("cookies", [])),
            self._user_agent[:80],
        )

    def get_json(
        self,
        url: str,
        params: dict[str, str] | list[tuple[str, str]] | None = None,
        *,
        base_url: str,
    ) -> Any:
        """
        GET *url* with solved CF cookies; returns parsed JSON.
        Warms up *base_url* on first call; re-warms once on CF wall.
        """
        if base_url not in self._warmed:
            self.warmup(base_url)

        resp = self._client.get(url, params=params, headers={"Accept": "application/json"})

        if self._is_cf_wall(resp):
            log.warning("action=cf_wall url=%s — re-warming and retrying", url)
            self._warmed.discard(base_url)
            self.warmup(base_url)
            resp = self._client.get(url, params=params, headers={"Accept": "application/json"})

        resp.raise_for_status()
        return resp.json()  # type: ignore[return-value]

    def get_html(
        self,
        url: str,
        params: dict[str, str] | None = None,
        *,
        base_url: str,
    ) -> str:
        """
        GET *url* with solved CF cookies; returns raw HTML text.
        Warms up *base_url* on first call; re-warms once on CF wall.
        """
        if base_url not in self._warmed:
            self.warmup(base_url)

        html_accept = {"Accept": "text/html,application/xhtml+xml,*/*"}
        resp = self._client.get(url, params=params, headers=html_accept)

        if self._is_cf_wall(resp):
            log.warning("action=cf_wall url=%s — re-warming and retrying", url)
            self._warmed.discard(base_url)
            self.warmup(base_url)
            resp = self._client.get(url, params=params, headers=html_accept)

        resp.raise_for_status()
        return resp.text

    def fetch_via_flaresolverr(self, url: str, *, session: str | None = None) -> str:
        """
        Fetch *url* directly through FlareSolverr's browser and return the
        fully-rendered HTML.  Slower than cookie reuse (one browser launch per
        call) but guaranteed to clear any CF/DataDome challenge.

        Pass a *session* id (from ``create_fs_session``) to reuse an existing
        browser instead of launching a fresh one for each call.
        """
        log.info("action=flaresolverr_direct_fetch url=%s session=%s", url, session)
        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": self._max_timeout_ms,
        }
        if session:
            payload["session"] = session
        data = self._call_flaresolverr(payload)
        return data["solution"]["response"]  # type: ignore[return-value]

    def create_fs_session(self) -> str:
        """
        Create a persistent FlareSolverr browser session.  Returns the session
        id string.  Use with ``fetch_via_flaresolverr(url, session=id)`` to
        reuse the same browser across multiple requests (much faster).
        """
        data = self._call_flaresolverr({"cmd": "sessions.create"})
        session_id: str = data["session"]
        log.info("action=flaresolverr_session_created session=%s", session_id)
        return session_id

    def destroy_fs_session(self, session_id: str) -> None:
        """Destroy a FlareSolverr browser session (best-effort cleanup)."""
        try:
            self._call_flaresolverr({"cmd": "sessions.destroy", "session": session_id})
            log.info("action=flaresolverr_session_destroyed session=%s", session_id)
        except FlareSolverrError:
            log.warning("action=flaresolverr_session_destroy_failed session=%s", session_id)
