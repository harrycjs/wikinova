"""WikiStore: durable storage for LLM-generated wiki pages.

Mirrors the structural patterns of ``nanobot.agent.memory.MemoryStore``:

- Atomic file writes (tempfile + ``os.replace`` + ``os.fsync``).
- ``GitStore``-backed audit log for ``wiki/pages/*.md`` and ``wiki/index.json``.
- Cursor / index state persisted as JSON.

The store is intentionally NOT a database — it is a flat namespace of
``<slug>.md`` files plus a single ``index.json``. The index is rebuilt
deterministically from disk on any inconsistency, so it can never drift
permanently from the page files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from nanobot.agent.wiki.frontmatter import (
    WikiFrontmatter,
    extract_wikilinks,
    parse_frontmatter,
    render_page,
)
from nanobot.agent.wiki.paths import WikiPaths
from nanobot.utils.gitstore import GitStore

# Pages tracked by the audit log. We deliberately do not track
# ``.evolution_cursor`` / ``.evolution_log.jsonl`` / ``.obsidian_state.json``
# so those bookkeeping writes do not themselves look like productive edits
# in the ``summarize_working_tree`` diff gate.
_TRACKED_WIKI_PATHS: tuple[str, ...] = (
    "wiki/index.json",
    "wiki/.evolution_cursor",
)


@dataclass
class WikiPage:
    """In-memory representation of a wiki page."""

    slug: str
    title: str
    body: str
    fm: WikiFrontmatter
    sha: str = ""
    mtime: str = ""
    backlinks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "sha": self.sha,
            "mtime": self.mtime,
            "tags": list(self.fm.tags),
            "links": list(self.fm.links),
        }


class WikiStore:
    """File-backed storage for wiki pages."""

    def __init__(self, paths: WikiPaths, *, git: GitStore | None = None):
        self.paths = paths
        self._write_lock = threading.Lock()
        self._async_write_lock: asyncio.Lock | None = None
        # Git audit is best-effort — wikis can run without git initialized.
        self._git = git or GitStore(
            paths.workspace,
            tracked_files=list(_TRACKED_WIKI_PATHS) + ["wiki/pages/*.md"],
        )
        # In-memory cache of the index for fast reads.
        self._index: dict[str, dict[str, Any]] | None = None

    # -- index management ---------------------------------------------------

    def _ensure_index_loaded(self) -> dict[str, dict[str, Any]]:
        if self._index is not None:
            return self._index
        if self.paths.index_file.exists():
            try:
                loaded = json.loads(self.paths.index_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._index = loaded
                    return self._index
            except (OSError, json.JSONDecodeError):
                logger.warning("wiki index.json corrupt; rebuilding from disk")
        # Fall back to a full rebuild from on-disk pages.
        self._index = self._rebuild_index_from_disk()
        self._save_index_unlocked()
        return self._index

    def _rebuild_index_from_disk(self) -> dict[str, dict[str, Any]]:
        rebuilt: dict[str, dict[str, Any]] = {}
        if not self.paths.pages_dir.exists():
            return rebuilt
        for page_path in sorted(self.paths.pages_dir.glob("*.md")):
            if page_path.name.endswith(".md.deleted"):
                continue
            slug = page_path.stem
            try:
                page = self._read_page_file(page_path, slug)
            except Exception:  # noqa: BLE001 — keep the rebuild best-effort.
                logger.exception("failed to read wiki page {} during index rebuild", slug)
                continue
            rebuilt[slug] = page.to_dict()
        return rebuilt

    def _save_index_unlocked(self) -> None:
        """Write the in-memory index to disk atomically. Caller holds the lock."""
        index = self._index or {}
        tmp = self.paths.index_file.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.paths.index_file)
            with open(self.paths.index_file, "ab") as fh:
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            logger.exception("failed to persist wiki index.json")

    def rebuild_index(self) -> dict[str, dict[str, Any]]:
        """Force-rebuild the index from on-disk pages (e.g. after external edits)."""
        with self._write_lock:
            self._index = self._rebuild_index_from_disk()
            self._save_index_unlocked()
            return dict(self._index)

    # -- reading -----------------------------------------------------------

    def list_pages(self) -> list[dict[str, Any]]:
        """Return all pages from the index, sorted by title."""
        with self._write_lock:
            index = self._ensure_index_loaded()
        return sorted(index.values(), key=lambda d: (d.get("title") or d.get("slug") or "").lower())

    def page_exists(self, slug: str) -> bool:
        with self._write_lock:
            index = self._ensure_index_loaded()
        return slug in index

    def read_page(self, slug: str) -> WikiPage | None:
        """Read a wiki page by slug, including body + parsed frontmatter."""
        with self._write_lock:
            self._ensure_index_loaded()
            page_path = self.paths.page_path(slug)
        if not page_path.exists():
            return None
        return self._read_page_file(page_path, slug)

    def _read_page_file(self, page_path: Path, slug: str) -> WikiPage:
        raw = page_path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)
        if not fm.slug:
            fm.slug = slug
        if not fm.title:
            fm.title = slug.replace("-", " ").title()
        sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        stat = page_path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        return WikiPage(
            slug=slug,
            title=fm.title,
            body=body,
            fm=fm,
            sha=sha,
            mtime=mtime,
        )

    # -- writing -----------------------------------------------------------

    def write_page(
        self,
        slug: str,
        title: str,
        body: str,
        *,
        tags: Iterable[str] | None = None,
        links: Iterable[str] | None = None,
        source: str = "",
        merge_existing: bool = True,
    ) -> WikiPage:
        """Create or update a wiki page atomically.

        ``merge_existing=True`` preserves ``created`` and merges tags/links with
        the existing page so a re-write doesn't drop metadata.
        """
        if not slug or not _SLUG_OK_RE.fullmatch(slug):
            raise ValueError(f"invalid slug: {slug!r}")

        with self._write_lock:
            self._ensure_index_loaded()
            existing = self._read_page_file_unlocked(slug)

            if existing and merge_existing:
                fm = existing.fm
                if title and title != fm.title:
                    fm.title = title
                if tags is not None:
                    fm.tags = _merge_unique(fm.tags, list(tags))
                if links is not None:
                    fm.links = _merge_unique(fm.links, list(links))
                if source:
                    fm.source = source
            else:
                fm = WikiFrontmatter(
                    title=title or slug.replace("-", " ").title(),
                    slug=slug,
                    tags=list(tags or []),
                    links=list(links or []),
                    source=source,
                )

            # Auto-detect links from the body if not explicitly provided.
            body_links = extract_wikilinks(body)
            if body_links:
                fm.links = _merge_unique(fm.links, body_links)

            rendered = render_page(fm, body)
            page_path = self.paths.page_path(slug)
            self._atomic_write_unlocked(page_path, rendered)

            sha = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
            stat = page_path.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            page = WikiPage(
                slug=slug,
                title=fm.title,
                body=body,
                fm=fm,
                sha=sha,
                mtime=mtime,
            )

            # Update index.
            assert self._index is not None
            self._index[slug] = page.to_dict()
            self._save_index_unlocked()
            return page

    def _read_page_file_unlocked(self, slug: str) -> WikiPage | None:
        """Read a page without acquiring the lock again. Caller holds it."""
        page_path = self.paths.page_path(slug)
        if not page_path.exists():
            return None
        return self._read_page_file(page_path, slug)

    def _atomic_write_unlocked(self, target: Path, content: str) -> None:
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
            with open(target, "ab") as fh:
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise

    def delete_page(self, slug: str) -> bool:
        """Soft-delete a wiki page by renaming it to ``<slug>.md.deleted``.

        Hard-delete requires explicit CLI action so the audit log always shows
        every removal.
        """
        with self._write_lock:
            self._ensure_index_loaded()
            page_path = self.paths.page_path(slug)
            if not page_path.exists():
                return False
            tombstone = self.paths.deleted_page_path(slug)
            if tombstone.exists():
                tombstone.unlink()
            page_path.rename(tombstone)
            assert self._index is not None
            self._index.pop(slug, None)
            self._save_index_unlocked()
            return True

    # -- backlinks ---------------------------------------------------------

    def backlinks(self, slug: str) -> list[str]:
        """Return the slugs of pages that link *to* ``slug``."""
        with self._write_lock:
            self._ensure_index_loaded()
            index = dict(self._index or {})
        out: list[str] = []
        for candidate_slug, entry in index.items():
            links = entry.get("links") or []
            if slug in links and candidate_slug != slug:
                out.append(candidate_slug)
        return out

    # -- git audit ---------------------------------------------------------

    @property
    def git(self) -> GitStore:
        return self._git

    def diff(self) -> str:
        """Return the structured diff of tracked wiki paths for cursor-advance gating."""
        if not self._git.is_initialized():
            return ""
        tracked = list(_TRACKED_WIKI_PATHS) + [self._page_glob_for_git()]
        return self._git.summarize_working_tree(tracked)

    def commit(self, message: str) -> None:
        """Best-effort auto-commit of the wiki state."""
        if not self._git.is_initialized():
            return
        try:
            self._git.auto_commit(message)
        except Exception:  # noqa: BLE001
            logger.exception("wiki auto_commit failed")

    def _page_glob_for_git(self) -> str:
        # GitStore.summarize_working_tree accepts literal paths plus globs.
        return "wiki/pages/*.md"


def _merge_unique(*lists: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for item in lst:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
    return out


_SLUG_OK_RE = re.compile(r"[a-z][a-z0-9-]{0,95}")
