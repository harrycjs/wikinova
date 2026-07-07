"""Minimal YAML frontmatter parser/serializer for wiki pages.

Avoids adding PyYAML as a dependency. Wiki pages use a small, fixed schema::

    ---
    title: "Foo"
    slug: "foo"
    tags: ["bar", "baz"]
    links: ["bar", "baz"]
    created: "2026-07-07T10:00:00"
    updated: "2026-07-07T10:00:00"
    source: "obsidian:Notes/foo.md"   # or "ima:capture-id" or "evolution"
    ---

The parser is permissive — values that fail to parse fall back to safe defaults
rather than raising, so a malformed file can still be read and re-saved.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# Match the first ``---``-delimited block at the top of the file.
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<body>.*?)\n---\s*\n?(?P<rest>.*)",
    re.DOTALL,
)

_LIST_RE = re.compile(r"^\s*-\s+(?P<v>.+?)\s*$")
_KV_RE = re.compile(r"^(?P<k>[A-Za-z_][\w-]*)\s*:\s*(?P<v>.*?)\s*$")


@dataclass
class WikiFrontmatter:
    """Parsed frontmatter for a wiki page."""

    title: str = ""
    slug: str = ""
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WikiFrontmatter":
        return cls(
            title=str(data.get("title", "")),
            slug=str(data.get("slug", "")),
            tags=list(data.get("tags") or []),
            links=list(data.get("links") or []),
            created=str(data.get("created", "")),
            updated=str(data.get("updated", "")),
            source=str(data.get("source", "")),
        )


def parse_frontmatter(text: str) -> tuple[WikiFrontmatter, str]:
    """Split a markdown document into (frontmatter, body).

    Returns an empty :class:`WikiFrontmatter` and the original text if no
    frontmatter block is found.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return WikiFrontmatter(), text

    body = match.group("body")
    rest = match.group("rest")
    data: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[str] | None = None
    pending_indent: int | None = None

    for raw_line in body.splitlines():
        if not raw_line.strip():
            current_list = None
            current_key = None
            pending_indent = None
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        # List item under a key
        list_match = _LIST_RE.match(raw_line.strip())
        if list_match and current_list is not None and indent >= (pending_indent or 0):
            current_list.append(_coerce_scalar(list_match.group("v")))
            continue

        kv_match = _KV_RE.match(raw_line.strip())
        if not kv_match:
            # Unknown line — reset state and move on.
            current_list = None
            current_key = None
            continue

        key, value = kv_match.group("k"), kv_match.group("v")
        current_key = key
        pending_indent = indent

        if value == "" or value is None:
            # Could be the start of a list block — we'll see on the next line.
            current_list = []
            data[key] = current_list
            continue

        # Inline JSON-style list ``[a, b, c]``
        if value.startswith("[") and value.endswith("]"):
            try:
                data[key] = json.loads(value.replace("'", '"'))
            except json.JSONDecodeError:
                # Try simple comma split as a last resort.
                inner = value[1:-1].strip()
                data[key] = [_coerce_scalar(p.strip()) for p in inner.split(",") if p.strip()]
            current_list = None
            continue

        data[key] = _coerce_scalar(value)
        current_list = None

    return WikiFrontmatter.from_dict(data), rest


def serialize_frontmatter(fm: WikiFrontmatter) -> str:
    """Serialize a :class:`WikiFrontmatter` block as YAML-ish text."""
    lines = ["---"]
    if fm.title:
        lines.append(f"title: {json.dumps(fm.title, ensure_ascii=False)}")
    if fm.slug:
        lines.append(f"slug: {fm.slug}")
    if fm.tags:
        lines.append("tags: " + json.dumps(fm.tags, ensure_ascii=False))
    if fm.links:
        lines.append("links: " + json.dumps(fm.links, ensure_ascii=False))
    if fm.created:
        lines.append(f"created: {fm.created}")
    if fm.updated:
        lines.append(f"updated: {fm.updated}")
    if fm.source:
        lines.append(f"source: {fm.source}")
    lines.append("---")
    return "\n".join(lines)


def render_page(
    fm: WikiFrontmatter,
    body: str,
    *,
    now: datetime | None = None,
) -> str:
    """Render a full wiki page (frontmatter + body) as a markdown string.

    Stamps ``updated`` (and ``created`` if empty) to ``now``.
    """
    ts = (now or datetime.now(timezone.utc)).isoformat()
    if not fm.created:
        fm.created = ts
    fm.updated = ts
    return serialize_frontmatter(fm) + "\n\n" + body.rstrip() + "\n"


def _coerce_scalar(value: str) -> Any:
    """Coerce a YAML-ish scalar to a Python value (str / int / float / bool / str)."""
    if value is None:
        return ""
    v = value.strip()
    if not v:
        return ""
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    if v.startswith("'") and v.endswith("'"):
        return v[1:-1]
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    if v.lower() in ("null", "~"):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


# ---------------------------------------------------------------------------
# Wikilink extraction
# ---------------------------------------------------------------------------

_WIKILINK_RE = re.compile(r"\[\[(?P<slug>[^\]|]+)(?:\|[^\]]+)?\]\]")


def extract_wikilinks(body: str) -> list[str]:
    """Return the unique wikilink slugs referenced in *body*, in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _WIKILINK_RE.finditer(body):
        raw = match.group("slug").strip()
        if not raw:
            continue
        # Strip Obsidian-style path prefixes (``Notes/foo`` → ``foo``).
        slug = raw.split("/")[-1].lower().replace(" ", "-")
        if slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out
