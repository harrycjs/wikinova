"""WikiQuerier: keyword (BM25) search over wiki pages.

Pure-Python BM25 implementation — no scipy / nltk dependency. The index is
rebuilt on demand whenever a page changes, and persisted to ``.bm25.pkl`` with
mtime invalidation so cold-starts are fast on large wikis.
"""

from __future__ import annotations

import math
import pickle
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from loguru import logger

from nanobot.agent.wiki.store import WikiStore

_TOKEN_RE = re.compile(r"[A-Za-z0-9一-鿿]+")


def tokenize(text: str) -> list[str]:
    """Split text into lowercase tokens.

    Chinese text is preserved per-character so e.g. "知识库" tokenizes into
    "知", "识", "库" — acceptable trade-off for keyword search without a
    proper Chinese segmenter.
    """
    if not text:
        return []
    out: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        chunk = match.group(0).lower()
        if not chunk:
            continue
        if any("一" <= ch <= "鿿" for ch in chunk):
            out.extend(chunk)
        else:
            out.append(chunk)
    return out


@dataclass(frozen=True)
class Hit:
    slug: str
    title: str
    snippet: str
    score: float

    def to_dict(self) -> dict:
        return {"slug": self.slug, "title": self.title, "snippet": self.snippet, "score": round(self.score, 4)}


class _Index:
    """In-memory BM25 index over wiki pages."""

    def __init__(self, pages: dict[str, dict], *, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs: dict[str, list[str]] = {}  # slug -> tokens
        self.doc_meta: dict[str, dict] = pages
        self.doc_len: dict[str, int] = {}
        self.avgdl: float = 0.0
        self.df: Counter[str] = Counter()  # document frequency
        self.tf: dict[str, Counter[str]] = {}  # per-doc term frequency
        self.n_docs: int = 0
        self._build()

    def _build(self) -> None:
        for slug, meta in self.doc_meta.items():
            text_parts = [meta.get("title", ""), " ".join(meta.get("tags") or [])]
            # Use a small body snippet so the index doesn't blow up on big pages.
            body = (meta.get("body") or "")[:8000]
            text_parts.append(body)
            tokens = tokenize(" ".join(text_parts))
            self.docs[slug] = tokens
            self.doc_len[slug] = len(tokens)
            counts = Counter(tokens)
            self.tf[slug] = counts
            for term in counts:
                self.df[term] += 1
        self.n_docs = max(1, len(self.doc_meta))
        total = sum(self.doc_len.values())
        self.avgdl = total / self.n_docs if self.n_docs else 0.0

    def search(self, query: str, k: int = 5) -> list[Hit]:
        q_tokens = tokenize(query)
        if not q_tokens or not self.docs:
            return []

        scores: dict[str, float] = {}
        for term in q_tokens:
            # Match exact term first; fall back to prefix match so "transformer"
            # finds "transformers" without needing a real stemmer.
            candidate_terms = [term]
            if len(term) >= 4:
                candidate_terms.extend(
                    indexed_term
                    for indexed_term in self.df
                    if indexed_term.startswith(term) and indexed_term != term
                )

            seen_dfs: set[str] = set()
            for cand in candidate_terms:
                df = self.df.get(cand, 0)
                if df == 0 or cand in seen_dfs:
                    continue
                seen_dfs.add(cand)
                idf = math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))
                for slug, tf in self.tf.items():
                    f = tf.get(cand, 0)
                    if f == 0:
                        continue
                    dl = self.doc_len[slug]
                    norm = 1 - self.b + self.b * (dl / max(self.avgdl, 1.0))
                    # Prefix matches get a small discount so exact matches rank higher.
                    discount = 1.0 if cand == term else 0.5
                    s = idf * (f * (self.k1 + 1)) / (f + self.k1 * norm) * discount
                    scores[slug] = scores.get(slug, 0.0) + s

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:k]
        hits: list[Hit] = []
        for slug, score in ranked:
            meta = self.doc_meta.get(slug, {})
            hits.append(
                Hit(
                    slug=slug,
                    title=meta.get("title", slug),
                    snippet=_snippet(meta.get("body", ""), q_tokens),
                    score=score,
                )
            )
        return hits


def _snippet(body: str, query_tokens: Iterable[str], length: int = 240) -> str:
    """Return a short snippet around the first matching token, or the head."""
    if not body:
        return ""
    body = body.strip()
    lower = body.lower()
    best_pos = len(body)
    for tok in query_tokens:
        idx = lower.find(tok)
        if idx != -1 and idx < best_pos:
            best_pos = idx
    if best_pos == len(body):
        return body[:length].strip()
    start = max(0, best_pos - 60)
    end = min(len(body), start + length)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return (prefix + body[start:end].strip() + suffix).strip()


class WikiQuerier:
    """Search wrapper around :class:`WikiStore` with on-disk BM25 cache."""

    def __init__(self, store: WikiStore, *, cache_path: Path | None = None):
        self.store = store
        self.cache_path = cache_path or (store.paths.wiki_dir / ".bm25.pkl")
        self._index: _Index | None = None
        self._index_signature: tuple | None = None

    def _signature(self) -> tuple:
        """A signature of page set + mtimes; index invalidates when this changes."""
        sig = []
        for meta in self.store.list_pages():
            sig.append((meta["slug"], meta.get("mtime", ""), meta.get("sha", "")))
        return tuple(sorted(sig))

    def _load_or_build_index(self) -> _Index:
        sig = self._signature()
        # Try cache first.
        if self.cache_path.exists():
            try:
                cached = pickle.loads(self.cache_path.read_bytes())
                if cached.get("signature") == sig:
                    self._index = cached["index"]
                    self._index_signature = sig
                    return self._index
            except (OSError, pickle.UnpicklingError, KeyError):
                logger.debug("bm25 cache unreadable; rebuilding")

        # Build fresh index. We need body + tags, which the index.json doesn't
        # carry, so re-read each page once.
        pages: dict[str, dict] = {}
        for meta in self.store.list_pages():
            slug = meta["slug"]
            page = self.store.read_page(slug)
            if page is None:
                continue
            pages[slug] = {
                "title": page.title,
                "tags": list(page.fm.tags),
                "body": page.body,
            }
        self._index = _Index(pages)
        self._index_signature = sig
        # Best-effort cache write.
        try:
            self.cache_path.write_bytes(pickle.dumps({"signature": sig, "index": self._index}))
        except OSError:
            logger.debug("bm25 cache write failed (non-fatal)")
        return self._index

    def search(self, query: str, k: int = 5) -> list[Hit]:
        """Top-k hits for *query*. Empty query → empty result."""
        if not query or not query.strip():
            return []
        index = self._load_or_build_index()
        return index.search(query, k=k)

    def invalidate(self) -> None:
        """Drop the cached index so the next search rebuilds from disk."""
        self._index = None
        self._index_signature = None
        if self.cache_path.exists():
            try:
                self.cache_path.unlink()
            except OSError:
                pass
