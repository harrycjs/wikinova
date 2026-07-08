"""IMACaptureTool — operates the IMA OpenAPI on the user's behalf.

This tool is intentionally a thin wrapper over :class:`IMAClient`. The heavy
lifting (writing to Obsidian, summarizing via LLM) lives in
:mod:`nanobot.agent.knowledge.pipeline` so the tool surface stays simple and
predictable.

Tools exposed here:

- ``ima_list_knowledge_bases`` — list the user's IMA knowledge bases.
- ``ima_get_knowledge_list`` — browse a KB's contents.
- ``ima_search_knowledge`` — search inside a KB.
- ``ima_search_notes`` — search user notes by title.
- ``ima_get_note_content`` — read a note's body.
- ``ima_create_note`` — create a new note (UTF-8 validated).
- ``ima_append_note`` — append to an existing note (UTF-8 validated, sensitive).
- ``ima_status`` — credential / connectivity status check.
"""

from __future__ import annotations

import json
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.ima._client import IMAClient, IMAError
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema


def _serialize(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _build_client_from_ctx(ctx) -> IMAClient:
    root = getattr(ctx, "config", ctx)
    tools = getattr(root, "tools", None)
    ima_cfg = getattr(tools, "ima", None) if tools is not None else getattr(root, "ima", None)
    # Support both camelCase (from config.json) and snake_case
    client_id = getattr(ima_cfg, "client_id", None) or getattr(ima_cfg, "clientId", None)
    api_key = getattr(ima_cfg, "api_key", None) or getattr(ima_cfg, "apiKey", None)
    base_url = getattr(ima_cfg, "base_url", None) or getattr(ima_cfg, "baseUrl", None) or IMAClient().base_url
    timeout_s = float(getattr(ima_cfg, "timeout_s", 30.0))
    return IMAClient.from_env_or_files(
        client_id=client_id,
        api_key=api_key,
        base_url=base_url,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------
# Base class with shared client
# ---------------------------------------------------------------------------


class _IMABase(Tool):
    """All IMA tools share a single client built at construction time."""

    config_key = "ima"
    _plugin_discoverable = True
    _client: IMAClient | None = None

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        root = getattr(ctx, "config", ctx)
        tools = getattr(root, "tools", None)
        ima_cfg = getattr(tools, "ima", None) if tools is not None else getattr(root, "ima", None)
        return bool(getattr(ima_cfg, "enabled", False))

    @classmethod
    def create(cls, ctx: Any) -> "_IMABase":
        instance = cls()
        instance._client = _build_client_from_ctx(ctx)
        return instance


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema("Search query. Empty string lists all knowledge bases."),
        cursor=StringSchema("Pagination cursor from previous response."),
        limit={"type": "string", "description": "Max results to return.", "default": "20"},
    )
)
class IMAListKnowledgeBasesTool(_IMABase):
    name = "ima_list_knowledge_bases"
    description = (
        "List or search the user's Tencent IMA knowledge bases. Pass an empty "
        "query to list all bases. Use this to discover which KBs the user has."
    )
    read_only = True
    concurrency_safe = True

    async def execute(self, query: str = "", cursor: str = "", limit: str = "20", **kwargs) -> str:
        try:
            top_k = max(1, min(50, int(limit)))
        except ValueError:
            top_k = 20
        try:
            data = await self._client.search_knowledge_base(query=query, cursor=cursor, limit=top_k)  # type: ignore[union-attr]
        except IMAError as exc:
            return ToolResult.error(f"IMA list_knowledge_bases failed: {exc}")
        return _serialize({"ok": True, "data": data})


@tool_parameters(
    tool_parameters_schema(
        kb_id=StringSchema("Knowledge base id (from ima_list_knowledge_bases)."),
        folder_id=StringSchema("Optional folder id; omit to list root."),
        cursor=StringSchema("Pagination cursor from previous response."),
        limit={"type": "string", "description": "Max items to return.", "default": "50"},
    )
)
class IMAGetKnowledgeListTool(_IMABase):
    name = "ima_get_knowledge_list"
    description = "Browse a knowledge base's contents (cursor-paginated)."
    read_only = True
    concurrency_safe = True

    async def execute(self, kb_id: str, folder_id: str = "", cursor: str = "", limit: str = "50", **kwargs) -> str:
        try:
            top_k = max(1, min(50, int(limit)))
        except ValueError:
            top_k = 50
        try:
            data = await self._client.get_knowledge_list(  # type: ignore[union-attr]
                kb_id=kb_id,
                folder_id=folder_id or None,
                cursor=cursor,
                limit=top_k,
            )
        except IMAError as exc:
            return ToolResult.error(f"IMA get_knowledge_list failed: {exc}")
        return _serialize({"ok": True, "data": data})


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema("Search query."),
        kb_id=StringSchema("Knowledge base id."),
        cursor=StringSchema("Pagination cursor."),
    )
)
class IMASearchKnowledgeTool(_IMABase):
    name = "ima_search_knowledge"
    description = "Search inside a specific IMA knowledge base."
    read_only = True
    concurrency_safe = True

    async def execute(self, query: str, kb_id: str, cursor: str = "", **kwargs) -> str:
        try:
            data = await self._client.search_knowledge(query=query, kb_id=kb_id, cursor=cursor)  # type: ignore[union-attr]
        except IMAError as exc:
            return ToolResult.error(f"IMA search_knowledge failed: {exc}")
        return _serialize({"ok": True, "data": data})


@tool_parameters(
    tool_parameters_schema(
        title=StringSchema("Note title to search for."),
        start={"type": "string", "description": "Pagination start offset.", "default": "0"},
        end={"type": "string", "description": "Pagination end offset.", "default": "20"},
    )
)
class IMASearchNotesTool(_IMABase):
    name = "ima_search_notes"
    description = "Search the user's IMA notes by title."
    read_only = True
    concurrency_safe = True

    async def execute(self, title: str, start: str = "0", end: str = "20", **kwargs) -> str:
        try:
            s = max(0, int(start))
            e = max(s + 1, min(100, int(end)))
        except ValueError:
            s, e = 0, 20
        try:
            data = await self._client.search_note(title=title, start=s, end=e)  # type: ignore[union-attr]
        except IMAError as exc:
            return ToolResult.error(f"IMA search_note failed: {exc}")
        return _serialize({"ok": True, "data": data})


@tool_parameters(
    tool_parameters_schema(
        note_id=StringSchema("Note id (from ima_search_notes)."),
        content_format={"type": "string", "description": "0 = plain text, 1 = markdown.", "default": "0"},
    )
)
class IMAGetNoteContentTool(_IMABase):
    name = "ima_get_note_content"
    description = "Read the body of an IMA note. content_format=0 returns plain text."
    read_only = True
    concurrency_safe = True

    async def execute(self, note_id: str, content_format: str = "0", **kwargs) -> str:
        try:
            fmt = int(content_format)
        except ValueError:
            fmt = 0
        try:
            data = await self._client.get_doc_content(note_id=note_id, content_format=fmt)  # type: ignore[union-attr]
        except IMAError as exc:
            return ToolResult.error(f"IMA get_doc_content failed: {exc}")
        return _serialize({"ok": True, "data": data})


@tool_parameters(
    tool_parameters_schema(
        content=StringSchema("Markdown body. MUST be valid UTF-8."),
        title=StringSchema("Note title (UTF-8)."),
        folder_id=StringSchema("Optional folder id."),
    )
)
class IMACreateNoteTool(_IMABase):
    name = "ima_create_note"
    description = (
        "Create a new IMA note. Content and title MUST be valid UTF-8. This is "
        "sensitive — confirm with the user before invoking."
    )
    read_only = False
    concurrency_safe = False

    async def execute(self, content: str, title: str = "", folder_id: str = "", **kwargs) -> str:
        try:
            data = await self._client.import_doc(  # type: ignore[union-attr]
                content=content,
                title=title or None,
                folder_id=folder_id or None,
            )
        except IMAError as exc:
            return ToolResult.error(f"IMA import_doc failed: {exc}")
        return _serialize({"ok": True, "data": data})


@tool_parameters(
    tool_parameters_schema(
        note_id=StringSchema("Existing note id to append to."),
        content=StringSchema("Markdown content to append. MUST be valid UTF-8."),
    )
)
class IMAAppendNoteTool(_IMABase):
    name = "ima_append_note"
    description = (
        "Append content to an existing IMA note. Sensitive: confirm the note_id "
        "with the user first — appending is not reversible."
    )
    read_only = False
    concurrency_safe = False

    async def execute(self, note_id: str, content: str, **kwargs) -> str:
        try:
            data = await self._client.append_doc(note_id=note_id, content=content)  # type: ignore[union-attr]
        except IMAError as exc:
            return ToolResult.error(f"IMA append_doc failed: {exc}")
        return _serialize({"ok": True, "data": data})


@tool_parameters(
    tool_parameters_schema()
)
class IMAStatusTool(_IMABase):
    name = "ima_status"
    description = "Return IMA credentials / connectivity status."
    read_only = True
    concurrency_safe = True

    async def execute(self, **kwargs) -> str:
        info = {
            "enabled": True,
            "has_credentials": bool(self._client and self._client.has_credentials()),  # type: ignore[union-attr]
            "base_url": getattr(self._client, "base_url", None) if self._client else None,
        }
        return _serialize({"ok": True, "data": info})
