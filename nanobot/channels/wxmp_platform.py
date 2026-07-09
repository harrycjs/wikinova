"""WeChat Official Account Platform (微信公众平台) channel.

This channel is NOT a chat platform — it's an authentication bridge that
captures a WeChat operator's session cookies via QR-scan login, then
reuses them so the IMA pipeline can fetch full article bodies for any
``https://mp.weixin.qq.com/s/...`` URL.

Rationale
---------

The ``mp.weixin.qq.com/s/<id>`` article pages are JS-rendered SPAs.
A plain ``httpx.get()`` against them returns a JS bundle, not the
article text. However, when the request is authenticated as an
operator (i.e., its cookies come from a logged-in 微信公众平台
session), the server returns SSR'd HTML containing the rendered
``<div id="js_content">``. The IMA ``get_doc_content`` OpenAPI call
returns ``210005 GetNoteContent not author`` for these items because
the OpenAPI token is not the KB owner. So this channel's sole purpose
is to hand-craft an authenticated fetch that the OpenAPI cannot do.

Login flow
----------

1. Launch Microsoft Edge via Playwright (``channel="msedge"`` so the
   installed Edge is reused — no Playwright-bundled Chromium download).
2. Navigate to ``https://mp.weixin.qq.com/``. The page shows a QR code.
3. User scans the QR with their WeChat mobile app.
4. After scan, the page redirects to ``/cgi-bin/home?token=<digits>``.
5. We capture all Playwright-context cookies + the URL token, persist
   them to ``<workspace>/wxmp_platform/``, and close the browser.

Fetching
---------

Module-level :func:`fetch_wxmp_article` reads the persisted
``wxmp_article_login.json`` and issues a plain ``httpx.get()`` with the
cookies injected into headers. The resulting HTML contains the
``<div id="js_content">`` block ready for the pipeline to extract.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

_STATE_FILENAME = "wxmp_article_login.json"
_LOGIN_TTL_S = 96 * 3600  # 4 days, matches WeMediaSpider-Python default


@dataclass
class WxMpCredentials:
    """Persisted WeChat MP operator session."""

    token: str
    cookies: dict[str, str] = field(default_factory=dict)
    login_at: int = 0  # unix seconds

    @property
    def is_fresh(self) -> bool:
        return bool(self.token) and (time.time() - self.login_at) < _LOGIN_TTL_S


def _state_dir(workspace: Path | None = None) -> Path:
    """Where the wxmp credentials live. Defaults to ``~/.nanobot/wxmp_article/``."""
    base = workspace or Path.home() / ".nanobot"
    d = base / "wxmp_article"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path(workspace: Path | None = None) -> Path:
    return _state_dir(workspace) / _STATE_FILENAME


def load_credentials(workspace: Path | None = None) -> WxMpCredentials | None:
    """Load credentials from disk. Returns ``None`` if missing or expired."""
    path = _state_path(workspace)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("wxmp_article: failed to read {}: {}", path, exc)
        return None
    cred = WxMpCredentials(
        token=data.get("token", ""),
        cookies={str(k): str(v) for k, v in (data.get("cookies") or {}).items()},
        login_at=int(data.get("login_at", 0)),
    )
    if not cred.is_fresh:
        logger.debug("wxmp_article: stored credentials expired")
        return None
    return cred


def save_credentials(creds: WxMpCredentials, workspace: Path | None = None) -> Path:
    """Persist credentials to disk and return the file path."""
    path = _state_path(workspace)
    path.write_text(
        json.dumps(
            {
                "token": creds.token,
                "cookies": creds.cookies,
                "login_at": creds.login_at,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("wxmp_article: saved credentials to {}", path)
    return path


def clear_credentials(workspace: Path | None = None) -> bool:
    """Remove persisted credentials if present. Returns True if a file was deleted."""
    path = _state_path(workspace)
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Public fetch utility (used by pipeline)
# ---------------------------------------------------------------------------


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://mp.weixin.qq.com/",
}


def fetch_wxmp_article(url: str, *, workspace: Path | None = None, timeout: float = 20.0) -> str | None:
    """Fetch a WeChat article URL using persisted operator cookies.

    Returns the full HTML page text (includes ``<div id="js_content">``)
    on success, ``None`` if there are no fresh credentials or the
    fetch fails. Designed as a synchronous helper so the pipeline's
    async path can wrap it with :func:`asyncio.to_thread`.
    """
    import httpx

    creds = load_credentials(workspace)
    if creds is None:
        return None
    headers = dict(HEADERS_BASE)
    headers["Cookie"] = _cookie_header(creds.cookies)
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except Exception as exc:
        logger.debug("wxmp_article: fetch failed for {}: {}", url[:80], exc)
        return None
    if resp.status_code != 200:
        logger.debug("wxmp_article: HTTP {} for {}", resp.status_code, url[:80])
        return None
    # Quick anti-bot wall check (the cookie auth shouldn't hit it but be safe)
    head = resp.text[:1500]
    if any(s in head for s in ("环境异常", "环境校验", "请在微信中打开", "请完成验证")):
        logger.debug("wxmp_article: still got anti-bot page for {}", url[:80])
        return None
    return resp.text


def extract_article_body(html: str) -> tuple[str, str]:
    """Best-effort extract of WeChat article title and body from the
    authenticated SSR'd page HTML.

    Returns (title, body_text). The body is paragraph-level plain text;
    images / HTML markup are intentionally stripped because the
    pipeline feeds it to an LLM for summarisation.

    Block-level HTML elements (``<p>``, ``<h*>``, ``<section>``, ``<br>``)
    become paragraph separators in the output so the LLM sees readable
    structured text.
    """
    title, body_html = "", ""
    m = re.search(r'<h1[^>]*id="activity-name"[^>]*>(.*?)</h1>', html, re.DOTALL)
    if m:
        title = _strip_tags(m.group(1)).strip()
    if not title:
        m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
        if m:
            title = m.group(1).strip()

    m = re.search(r'<div[^>]+id="js_content"[^>]*>(.*?)</div>\s*<script', html, re.DOTALL)
    if m:
        body_html = m.group(1)
    else:
        # Looser fallback — anything inside the js_content container
        m = re.search(r'<div[^>]+id="js_content"[^>]*>(.*)', html, re.DOTALL)
        if m:
            body_html = m.group(1)

    if not body_html:
        return title, ""

    # Block-level endings → paragraph separators BEFORE we strip tags
    # (otherwise the _strip_tags regex removes the sentinel too).
    body_html = re.sub(
        r"</(?:p|h[1-6]|section|div|li|tr|blockquote|pre|ul|ol)\s*>",
        "\n\n",
        body_html,
        flags=re.IGNORECASE,
    )
    body_html = re.sub(r"<\s*br\s*/?\s*>", "\n", body_html, flags=re.IGNORECASE)

    body = _strip_tags(body_html)
    body = re.sub(r"[\t]+", " ", body)
    body = re.sub(r"[ ]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return title, body


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(html: str) -> str:
    return _TAG_RE.sub("", html or "")


# ---------------------------------------------------------------------------
# Channel class (login only — this channel does not handle messages)
# ---------------------------------------------------------------------------


class WxMpPlatformChannel(BaseChannel):
    """Persistent WeChat MP operator session.

    Inherits :class:`BaseChannel` so it plugs into the existing
    ``nanobot channels login <name>`` CLI without any CLI changes, but
    it does NOT participate in the message bus — its sole runtime
    responsibility is the QR-scan login flow that populates
    ``wxmp_article_login.json``.
    """

    name = "wxmp_article"
    display_name = "WeChat Official Account Platform (operator session)"

    def __init__(self, config: Any, bus: MessageBus | None = None):
        # BaseChannel.__init__ requires a bus; we don't send messages
        # so accept None and just skip the wiring.
        super().__init__(config or {}, bus=bus or MessageBus())
        self._workspace = Path(getattr(config, "workspace", None) or Path.home() / ".nanobot") if config else Path.home() / ".nanobot"

    # BaseChannel subclassing requires start()/stop()/send() — but this
    # channel never actually runs as a chat channel. Implement no-ops
    # that satisfy the abstract methods.
    async def start(self) -> None:  # pragma: no cover - never called
        return None

    async def stop(self) -> None:  # pragma: no cover - never called
        return None

    async def send(self, msg) -> None:  # pragma: no cover - never called
        return None

    async def login(self, force: bool = False) -> bool:
        """Run the QR-scan login flow.

        ``force=True`` clears existing credentials first. Blocks until
        the user scans (or 10-minute timeout) — the caller should run
        this in a foreground thread / interactive command.
        """
        return await asyncio.to_thread(self._login_sync, force)

    def _login_sync(self, force: bool) -> bool:
        """Synchronous login implementation (runs in worker thread)."""
        if not force and load_credentials(self._workspace) is not None:
            logger.info("wxmp_article: already logged in; pass --force to re-auth")
            print("Already logged in. Use --force to re-authenticate.")
            return True

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("wxmp_article: playwright not installed. Run: pip install playwright")
            print("ERROR: playwright not installed. Run: pip install playwright")
            return False

        deadline = time.time() + 600  # 10 minutes
        try:
            with sync_playwright() as p:
                print("[wxmp_article] Launching Microsoft Edge...", flush=True)
                browser = p.chromium.launch(
                    channel="msedge",
                    headless=False,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                try:
                    context = browser.new_context(
                        user_agent=HEADERS_BASE["User-Agent"],
                    )
                    page = context.new_page()
                    print("[wxmp_article] Navigating to mp.weixin.qq.com...", flush=True)
                    page.goto(
                        "https://mp.weixin.qq.com/",
                        wait_until="domcontentloaded",
                    )
                    print(
                        "[wxmp_article] Page loaded. Please scan the QR code "
                        "with your WeChat mobile app.",
                        flush=True,
                    )

                    while time.time() < deadline:
                        try:
                            page.wait_for_url(
                                "**/cgi-bin/home**token=**", timeout=2000
                            )
                        except Exception:
                            # Not logged in yet; keep waiting
                            time.sleep(1)
                            continue

                        url = page.url
                        print(f"[wxmp_article] Login detected: {url}", flush=True)
                        m = re.search(r"token=(\d+)", url)
                        if not m:
                            print(
                                "[wxmp_article] URL has no token — aborting",
                                flush=True,
                            )
                            return False
                        token = m.group(1)
                        cookies = {
                            c["name"]: c["value"] for c in context.cookies()
                        }
                        cred = WxMpCredentials(
                            token=token,
                            cookies=cookies,
                            login_at=int(time.time()),
                        )
                        save_credentials(cred, self._workspace)
                        print(
                            f"[wxmp_article] Saved token ({len(cookies)} cookies)",
                            flush=True,
                        )
                        return True

                    print("[wxmp_article] Timed out waiting for QR scan", flush=True)
                    return False
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as exc:
            logger.exception("wxmp_article: login failed")
            print(f"[wxmp_article] Login failed: {exc}", flush=True)
            return False
