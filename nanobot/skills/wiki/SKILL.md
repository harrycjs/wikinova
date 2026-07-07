---
name: wiki
description: Search, read, and (when authoring) update the LLM-generated wiki that mirrors the user's Obsidian vault. Always-available skill — the wiki is your primary grounding source.
always: true
---

# Wiki

The wiki at `workspace/wiki/pages/` is an LLM-generated, interconnected cache of
the user's Obsidian vault. Each page is one markdown file with YAML frontmatter
and `[[wikilink]]` references to related pages. The wiki is git-tracked; every
evolution pass produces a commit.

## Tools

- `wiki_search(query, k=5)` — ranked hits with slug, title, snippet, score.
- `wiki_read(slug)` — full body of a page.
- `wiki_backlinks(slug)` — pages that link to the given slug.
- `list_wiki_pages` — enumerate everything.

The Q&A agent (read-only role) has access to these. The generator role
additionally has `write_wiki_page` and `update_wiki_page` — never call these
from a regular user turn.

## Citing sources

Always cite wiki pages with `[wiki:slug-name]` syntax. Examples:

- "Based on `[wiki:nanobot-architecture]`, the bus decouples channels from the agent core."
- "See also `[wiki:agent-loop]`."

The WebUI renders these as hover-cards with a page preview.

## When the wiki is empty or unhelpful

If `wiki_search` returns no hits, say so honestly. Then either:

- Try `obsidian_search` to find the content directly in the raw vault.
- Suggest the user regenerate the wiki via `/api/wiki/regenerate`.

Never fall back to general training knowledge.

## Self-evolution

Every ~6 hours (configurable via `tools.wiki.evolution.interval_h`) the wiki
self-evolves: it reads recent `history.jsonl` entries, extracts new facts /
preferences / habits / entities, and writes them as wiki pages or appends them
to existing ones. Each pass is git-committed so it's fully auditable.