"""Obsidian tools — read-only access to the user's vault.

The agent MUST NOT write to the Obsidian vault directly. The only path that
produces new files inside the vault is the IMA → LLM → vault pipeline, which
writes into ``<vault>/<vault_root>/<inbox_subdir>/`` (default
``Nanobot/Inbox``) and never touches other directories.

Tools:

- ``obsidian_read`` — read a markdown file by vault-relative path.
- ``obsidian_list`` — list markdown files (optionally filtered by glob).
- ``obsidian_search`` — keyword search over vault files.
- ``obsidian_get_backlinks`` — find wiki-links pointing at a slug.

Security: every operation goes through :func:`require_path_within` so the
agent cannot escape the configured vault path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.security.workspace_policy import (
    WorkspaceBoundaryError,
    require_path_within,
)

_WIKILINK_RE = re.compile(r"\[\[(?P<slug>[^\]|]+)(?:\|[^\]]+)?\]\]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9一-鿿]+")


def _vault_path_from_ctx(ctx) -> Path | None:
    """Extract the configured vault path from a ToolContext-like object.

    The ``ctx`` may be either a full ``Config`` (with ``.tools.obsidian``), a
    ``ToolContext`` (with ``.config`` of any of the above shapes), or the
    ``ObsidianToolsConfig`` itself (when called from tests).
    """
    # Try ``ctx.config.tools.obsidian.vault_path`` / ``ctx.config.obsidian.vault_path``.
    root = getattr(ctx, "config", ctx)
    tools = getattr(root, "tools", None)
    obs_cfg = None
    if tools is not None:
        obs_cfg = getattr(tools, "obsidian", None)
    if obs_cfg is None:
        obs_cfg = getattr(root, "obsidian", None)
    if obs_cfg is None and hasattr(root, "vault_path"):
        obs_cfg = root  # ctx itself is the ObsidianToolsConfig.
    vault_path = getattr(obs_cfg, "vault_path", None) if obs_cfg is not None else None
    if not vault_path:
        return None
    return Path(vault_path).expanduser().resolve()


def _resolve_under_vault(vault_root: Path, raw_path: str) -> Path:
    """Resolve a user-supplied vault-relative path and verify it stays inside the vault."""
    if Path(raw_path).is_absolute():
        raise ValueError("Vault-relative path must not be absolute.")
    target = (vault_root / raw_path).resolve()
    try:
        require_path_within(target, vault_root)
    except WorkspaceBoundaryError as exc:
        raise ValueError(f"Refusing to read {raw_path}: outside vault ({exc}).") from exc
    return target


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase alphanumeric + per-character CJK."""
    if not text:
        return []
    out: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        chunk = match.group(0).lower()
        if not chunk:
            continue
        # Latin / digit chunks stay as a single token; CJK chunks split per-char
        # so e.g. "深度学习" tokenizes to ["深", "度", "学", "习"].
        if any("一" <= ch <= "鿿" for ch in chunk):
            out.extend(chunk)
        else:
            out.append(chunk)
    return out


def _search_in_files(root: Path, pattern: str, *, max_hits: int = 25) -> list[dict[str, Any]]:
    """Walk ``root`` and find markdown files whose body matches ``pattern``."""
    q_tokens = _tokenize(pattern)
    if not q_tokens:
        return []

    hits: list[dict[str, Any]] = []
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        body_tokens = _tokenize(text)
        if not body_tokens:
            continue
        body_set = set(body_tokens)
        matched = sum(1 for tok in q_tokens if tok in body_set)
        if matched == 0:
            continue
        score = matched / max(1, len(q_tokens))
        snippet = _snippet_around_token(text, q_tokens[0])
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        hits.append({"path": rel, "score": round(score, 3), "snippet": snippet})
        if len(hits) >= max_hits:
            break
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits


def _snippet_around_token(text: str, token: str, *, length: int = 240) -> str:
    idx = text.lower().find(token)
    if idx == -1:
        return text[:length].strip()
    start = max(0, idx - 60)
    end = min(len(text), start + length)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return (prefix + text[start:end].strip() + suffix).strip()


# ---------------------------------------------------------------------------
# Base class with shared vault path lookup
# ---------------------------------------------------------------------------


class _ObsidianBase(Tool):
    """Shared helpers: vault path resolution, JSON return formatting."""

    config_key = "obsidian"
    _plugin_discoverable = True
    _vault_root: Path | None = None

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        root = getattr(ctx, "config", ctx)
        tools = getattr(root, "tools", None)
        obs_cfg = getattr(tools, "obsidian", None) if tools is not None else getattr(root, "obsidian", None)
        return bool(getattr(obs_cfg, "enabled", False)) and bool(getattr(obs_cfg, "vault_path", None))

    @classmethod
    def create(cls, ctx: Any) -> "_ObsidianBase":
        instance = cls()
        instance._vault_root = _vault_path_from_ctx(ctx)
        return instance

    def _vault(self) -> Path | None:
        return self._vault_root

    def _err_vault_missing(self) -> str:
        return "Obsidian vault is not configured (tools.obsidian.vault_path)."


@tool_parameters(
    tool_parameters_schema(
        path=StringSchema("Vault-relative path to a markdown file (e.g. ``Notes/foo.md``)."),
    )
)
class ObsidianReadTool(_ObsidianBase):
    name = "obsidian_read"
    description = (
        "Read a single markdown file from the user's Obsidian vault. The path "
        "must be vault-relative (e.g. ``Nanobot/Inbox/2026-07-07-foo.md``). "
        "Refuses absolute paths or paths that escape the vault."
    )
    read_only = True
    concurrency_safe = True

    async def execute(self, path: str, **kwargs) -> str:
        root = self._vault()
        if root is None:
            return ToolResult.error(self._err_vault_missing())
        try:
            target = _resolve_under_vault(root, path)
        except ValueError as exc:
            return ToolResult.error(str(exc))
        if not target.exists() or not target.is_file():
            return ToolResult.error(f"File not found: {path}")
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult.error(f"Read failed: {exc}")
        return f"# {path}\n\n{content}"


@tool_parameters(
    tool_parameters_schema(
        glob={"type": "string", "description": "Glob pattern relative to vault.", "default": "**/*.md"},
        limit={"type": "string", "description": "Max files to return.", "default": "100"},
    )
)
class ObsidianListTool(_ObsidianBase):
    name = "obsidian_list"
    description = (
        "List markdown files inside the user's Obsidian vault. Optional glob "
        "filter (default ``**/*.md``). Read-only — does not modify anything."
    )
    read_only = True
    concurrency_safe = True

    async def execute(self, glob: str = "**/*.md", limit: str = "100", **kwargs) -> str:
        root = self._vault()
        if root is None:
            return ToolResult.error(self._err_vault_missing())
        try:
            cap = max(1, min(500, int(limit)))
        except ValueError:
            cap = 100
        try:
            files = sorted(root.glob(glob or "**/*.md"))
        except (OSError, ValueError) as exc:
            return ToolResult.error(f"List failed: {exc}")
        if not files:
            return f"No files match glob {glob!r} under {root}."
        out: list[dict[str, Any]] = []
        for path in files[:cap]:
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append({"path": rel, "size": stat.st_size, "mtime": int(stat.st_mtime)})
        if len(files) > cap:
            out.append({"truncated": True, "matched": len(files), "returned": cap})
        return json.dumps({"ok": True, "vault": str(root), "files": out}, ensure_ascii=False)


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema("Search query — keywords (Chinese or English)."),
        limit={"type": "string", "description": "Max hits.", "default": "25"},
    )
)
class ObsidianSearchTool(_ObsidianBase):
    name = "obsidian_search"
    description = (
        "Keyword search across the user's Obsidian vault. Use this when the "
        "wiki doesn't cover a topic — the raw vault is searched directly."
    )
    read_only = True
    concurrency_safe = True

    async def execute(self, query: str, limit: str = "25", **kwargs) -> str:
        root = self._vault()
        if root is None:
            return ToolResult.error(self._err_vault_missing())
        try:
            cap = max(1, min(100, int(limit)))
        except ValueError:
            cap = 25
        if not query.strip():
            return ToolResult.error("query is required")
        hits = _search_in_files(root, query, max_hits=cap)
        if not hits:
            return f"No Obsidian vault files matched {query!r}."
        return json.dumps({"ok": True, "query": query, "hits": hits}, ensure_ascii=False)


@tool_parameters(
    tool_parameters_schema(
        slug=StringSchema("Page slug to find backlinks for (lowercase, dashes)."),
    )
)
class ObsidianBacklinksTool(_ObsidianBase):
    name = "obsidian_get_backlinks"
    description = (
        "Find vault notes that contain a ``[[slug]]`` wikilink to the given slug. "
        "Use this to traverse the Obsidian knowledge graph directly."
    )
    read_only = True
    concurrency_safe = True

    async def execute(self, slug: str, **kwargs) -> str:
        root = self._vault()
        if root is None:
            return ToolResult.error(self._err_vault_missing())
        slug_norm = slug.strip().lower().split("/")[-1]
        if not slug_norm:
            return ToolResult.error("slug is required")

        out: list[dict[str, Any]] = []
        for path in root.rglob("*.md"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in _WIKILINK_RE.finditer(text):
                raw = match.group("slug").strip().lower()
                if raw.split("/")[-1] == slug_norm:
                    try:
                        rel = path.relative_to(root).as_posix()
                    except ValueError:
                        rel = path.as_posix()
                    out.append({"path": rel})
                    break
        if not out:
            return f"No Obsidian notes link to [[{slug_norm}]]."
        return json.dumps({"ok": True, "slug": slug_norm, "backlinks": out}, ensure_ascii=False)
