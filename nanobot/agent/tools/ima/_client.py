"""IMA OpenAPI client.

Implements the subset of the Tencent IMA OpenAPI used by nanobot:

- Knowledge Base module (``openapi/wiki/v1``):
    - ``search_knowledge_base`` — find KBs by name (empty query lists all)
    - ``get_knowledge_base`` — KB details
    - ``get_knowledge_list`` — browse KB contents (cursor + limit)
    - ``search_knowledge`` — search inside a KB
    - ``get_media_info`` — fetch a media item
- Notes module (``openapi/note/v1``):
    - ``list_notebook`` — list notebooks
    - ``list_note`` — list notes in a folder
    - ``search_note`` — search by title
    - ``get_doc_content`` — read a note's body
    - ``import_doc`` — create a new note (UTF-8 critical)
    - ``append_doc`` — append to an existing note (UTF-8 critical, sensitive)

Authentication: two headers — ``ima-openapi-clientid`` and ``ima-openapi-apikey``.
Credentials come from ``IMAToolsConfig`` first, then fall back to env vars
(``IMA_OPENAPI_CLIENTID`` / ``IMA_OPENAPI_APIKEY``) and finally to files at
``~/.config/ima/{client_id,api_key}`` (the canonical storage from the IMA
skill at ``~/.claude/skills/ima-skills``).

All endpoints use ``POST`` + JSON body. Responses follow::

    {"code": 0, "msg": "...", "data": {...}}

``code != 0`` indicates a backend error; we surface ``msg`` to the caller.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from nanobot.agent.tools.ima._encoding import best_effort_decode

DEFAULT_BASE_URL = "https://ima.qq.com"


class IMAError(Exception):
    """Raised for both transport and backend errors."""

    def __init__(self, code: int | str, message: str, *, raw: str | None = None):
        super().__init__(f"[code={code}] {message}")
        self.code = code
        self.message = message
        self.raw = raw


@dataclass
class IMAClient:
    """Async client for the IMA OpenAPI."""

    client_id: str | None = None
    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = 30.0

    http: httpx.AsyncClient = field(init=False)

    def __init__(self, client_id: str | None = None, api_key: str | None = None, base_url: str = DEFAULT_BASE_URL, timeout_s: float = 30.0, http: httpx.AsyncClient | None = None):
        self.client_id = client_id
        self.api_key = api_key
        self.base_url = base_url or DEFAULT_BASE_URL
        self.timeout_s = timeout_s
        self.http = http or httpx.AsyncClient(timeout=timeout_s)

    # -- credential loading ----------------------------------------------

    @classmethod
    def from_env_or_files(cls, **kwargs: Any) -> "IMAClient":
        """Build an IMAClient using the standard credential chain.

        Priority: explicit args → ``$IMA_OPENAPI_CLIENTID`` / ``$IMA_OPENAPI_APIKEY``
        → ``~/.config/ima/{client_id,api_key}``.
        """
        client_id = kwargs.pop("client_id", None) or os.environ.get("IMA_OPENAPI_CLIENTID") or os.environ.get("IMA_CLIENT_ID")
        api_key = kwargs.pop("api_key", None) or os.environ.get("IMA_OPENAPI_APIKEY") or os.environ.get("IMA_API_KEY")
        if not client_id or not api_key:
            cfg_dir = _home_dir() / ".config" / "ima"
            client_id = client_id or _read_credential_file(cfg_dir / "client_id")
            api_key = api_key or _read_credential_file(cfg_dir / "api_key")
        return cls(client_id=client_id, api_key=api_key, **kwargs)

    def has_credentials(self) -> bool:
        return bool(self.client_id and self.api_key)

    # -- core request ----------------------------------------------------

    async def _request(self, api_path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.has_credentials():
            raise IMAError(
                code=-100,
                message=(
                    "IMA credentials are missing. Set IMA_OPENAPI_CLIENTID and "
                    "IMA_OPENAPI_APIKEY, or place files at ~/.config/ima/{client_id,api_key}."
                ),
            )
        url = f"{self.base_url.rstrip('/')}/{api_path.lstrip('/')}"
        headers = {
            "ima-openapi-clientid": self.client_id or "",
            "ima-openapi-apikey": self.api_key or "",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = await self.http.post(
                url,
                headers=headers,
                content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            )
        except httpx.HTTPError as exc:
            raise IMAError(code=-100, message=f"transport error: {exc}") from exc

        raw_text = best_effort_decode(response.content)
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise IMAError(
                code=-100,
                message=f"non-JSON response from IMA: {exc}",
                raw=raw_text[:500],
            ) from exc

        if not isinstance(payload, dict):
            raise IMAError(code=-100, message="unexpected response shape", raw=raw_text[:500])

        code = payload.get("code", -1)
        if code != 0:
            raise IMAError(
                code=code,
                message=str(payload.get("msg") or payload.get("message") or "unknown IMA error"),
                raw=raw_text[:500],
            )
        return payload.get("data") or {}

    # -- knowledge-base helpers -----------------------------------------

    async def search_knowledge_base(self, query: str = "", *, cursor: str = "", limit: int = 20) -> dict[str, Any]:
        return await self._request(
            "openapi/wiki/v1/search_knowledge_base",
            {"query": query, "cursor": cursor, "limit": min(max(limit, 1), 50)},
        )

    async def get_knowledge_base(self, kb_id: str) -> dict[str, Any]:
        return await self._request("openapi/wiki/v1/get_knowledge_base", {"ids": [kb_id]})

    async def get_knowledge_list(
        self, kb_id: str, *, folder_id: str | None = None, cursor: str = "", limit: int = 50
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"knowledge_base_id": kb_id, "cursor": cursor, "limit": min(max(limit, 1), 50)}
        if folder_id:
            body["folder_id"] = folder_id
        return await self._request("openapi/wiki/v1/get_knowledge_list", body)

    async def search_knowledge(self, query: str, kb_id: str, *, cursor: str = "") -> dict[str, Any]:
        return await self._request(
            "openapi/wiki/v1/search_knowledge",
            {"query": query, "knowledge_base_id": kb_id, "cursor": cursor},
        )

    async def get_media_info(self, media_id: str) -> dict[str, Any]:
        return await self._request("openapi/wiki/v1/get_media_info", {"media_id": media_id})

    # -- notes helpers --------------------------------------------------

    async def list_notebook(self, *, cursor: str = "0", limit: int = 50) -> dict[str, Any]:
        return await self._request(
            "openapi/note/v1/list_notebook",
            {"cursor": cursor, "limit": min(max(limit, 1), 100)},
        )

    async def list_note(
        self,
        *,
        folder_id: str | None = None,
        cursor: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"cursor": cursor, "limit": min(max(limit, 1), 100)}
        if folder_id:
            body["folder_id"] = folder_id
        return await self._request("openapi/note/v1/list_note", body)

    async def search_note(
        self,
        title: str,
        *,
        start: int = 0,
        end: int = 20,
    ) -> dict[str, Any]:
        return await self._request(
            "openapi/note/v1/search_note",
            {"search_type": 0, "query_info": {"title": title}, "start": start, "end": end},
        )

    async def get_doc_content(self, note_id: str, *, content_format: int = 0) -> dict[str, Any]:
        return await self._request(
            "openapi/note/v1/get_doc_content",
            {"note_id": note_id, "target_content_format": content_format},
        )

    async def import_doc(self, content: str, *, title: str | None = None, folder_id: str | None = None) -> dict[str, Any]:
        """Create a new note. ``content`` and ``title`` MUST be valid UTF-8."""
        from nanobot.agent.tools.ima._encoding import ensure_utf8

        ensure_utf8(content, field="content")
        if title:
            ensure_utf8(title, field="title")
        body: dict[str, Any] = {"content": content, "content_format": 1}
        if title:
            body["title"] = title
        if folder_id:
            body["folder_id"] = folder_id
        return await self._request("openapi/note/v1/import_doc", body)

    async def append_doc(self, note_id: str, content: str) -> dict[str, Any]:
        """Append *content* to an existing note. Sensitive — caller must confirm."""
        from nanobot.agent.tools.ima._encoding import ensure_utf8

        ensure_utf8(content, field="content")
        return await self._request(
            "openapi/note/v1/append_doc",
            {"note_id": note_id, "content": content, "content_format": 1},
        )

    # -- lifecycle -------------------------------------------------------

    async def aclose(self) -> None:
        await self.http.aclose()


def _read_credential_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return text or None


def _home_dir() -> Path:
    """Return the user's home dir, honoring HOME on POSIX and USERPROFILE on Windows."""
    return Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or Path.home())
