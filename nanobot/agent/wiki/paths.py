"""Path layout for the LLM-generated wiki cache.

All paths are derived from the user's workspace root (``~/.nanobot/workspace``).
This module is the single source of truth for where wiki state lives so that
``WikiStore``, ``WikiQuerier``, ``WikiGenerator`` and ``WikiEvolution`` agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nanobot.utils.helpers import ensure_dir


@dataclass(frozen=True)
class WikiPaths:
    """Filesystem layout for the wiki subsystem.

    Attributes
    ----------
    workspace : Path
        Root of the nanobot workspace (parent of the wiki directory).
    wiki_dir : Path
        ``<workspace>/wiki`` — root for all wiki state.
    pages_dir : Path
        ``<workspace>/wiki/pages`` — one ``<slug>.md`` per page.
    index_file : Path
        ``<workspace>/wiki/index.json`` — slug → {title, tags, links, sha, mtime}.
    evolution_cursor_file : Path
        ``<workspace>/wiki/.evolution_cursor`` — last processed history cursor.
    evolution_log_file : Path
        ``<workspace>/wiki/.evolution_log.jsonl`` — append-only audit of runs.
    obsidian_state_file : Path
        ``<workspace>/wiki/.obsidian_state.json`` — sha256 ETag per synced file.
    ima_captures_dir : Path
        ``<workspace>/ima/captures`` — raw IMA captures.
    ima_cursor_file : Path
        ``<workspace>/ima/.sync_cursor.json``.
    """

    workspace: Path
    wiki_dir: Path
    pages_dir: Path
    index_file: Path
    evolution_cursor_file: Path
    evolution_log_file: Path
    obsidian_state_file: Path
    ima_captures_dir: Path
    ima_cursor_file: Path

    @classmethod
    def from_workspace(cls, workspace: Path) -> "WikiPaths":
        """Build the layout from a workspace root, creating dirs as needed."""
        workspace = workspace.expanduser().resolve()
        wiki_dir = ensure_dir(workspace / "wiki")
        pages_dir = ensure_dir(wiki_dir / "pages")
        # index + cursors are files (created lazily); just touch parents.
        ensure_dir(workspace / "ima" / "captures")
        return cls(
            workspace=workspace,
            wiki_dir=wiki_dir,
            pages_dir=pages_dir,
            index_file=wiki_dir / "index.json",
            evolution_cursor_file=wiki_dir / ".evolution_cursor",
            evolution_log_file=wiki_dir / ".evolution_log.jsonl",
            obsidian_state_file=wiki_dir / ".obsidian_state.json",
            ima_captures_dir=workspace / "ima" / "captures",
            ima_cursor_file=workspace / "ima" / ".sync_cursor.json",
        )

    # Convenience helpers --------------------------------------------------

    def page_path(self, slug: str) -> Path:
        """Return the on-disk path for a wiki page by slug."""
        if not _SLUG_RE.fullmatch(slug):
            raise ValueError(f"invalid wiki page slug: {slug!r}")
        return self.pages_dir / f"{slug}.md"

    def deleted_page_path(self, slug: str) -> Path:
        """Return the soft-delete tombstone path for a wiki page."""
        return self.pages_dir / f"{slug}.md.deleted"

    def relative_to_workspace(self, path: Path) -> str:
        """POSIX-style path relative to ``workspace``; raises if not inside it."""
        try:
            return path.resolve().relative_to(self.workspace).as_posix()
        except ValueError as exc:
            raise ValueError(f"path {path} is not inside workspace {self.workspace}") from exc


# Slug validation: lowercase letters, digits, dashes; 1–96 chars. Must start with
# a letter. Mirrors Obsidian's own slug conventions so wiki slugs can be safely
# referenced from ``[[wikilinks]]``.
import re  # noqa: E402  (placed here to keep the dataclass block compact)

_SLUG_RE = re.compile(r"[a-z][a-z0-9-]{0,95}")
