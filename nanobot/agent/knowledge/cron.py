"""Knowledge sync cron job: IMA → Obsidian → Wiki.

Called every 6 hours via the cron system.  Fetches new IMA content,
writes summaries to the Obsidian Inbox, then scans the Obsidian vault
to regenerate wiki pages.
"""

from __future__ import annotations

import json as _json
import re as _re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx as _httpx
from loguru import logger


async def run_knowledge_sync(config: Any, agent: Any) -> None:
    """Full pipeline: IMA → Obsidian → Wiki sync."""
    from pathlib import Path as _Path

    ima_cfg = getattr(config.tools, "ima", None)
    obs_cfg = getattr(config.tools, "obsidian", None)

    # ── Step 1: IMA sync (fetch new notes into Obsidian Inbox) ──────────
    if getattr(ima_cfg, "enabled", False):
        await _sync_ima(config, ima_cfg, obs_cfg, _Path)

    # ── Step 2: Obsidian → Wiki sync ────────────────────────────────────
    if getattr(obs_cfg, "enabled", False) and getattr(obs_cfg, "vault_path", None):
        await _sync_obsidian_to_wiki(config, obs_cfg, _Path)

    logger.info("knowledge_sync: completed")


async def _sync_ima(config: Any, ima_cfg: Any, obs_cfg: Any, _Path: type) -> None:
    """Fetch new IMA items → Obsidian Inbox, skip already-processed items."""
    try:
        from nanobot.agent.tools.ima._client import IMAClient

        client = IMAClient.from_env_or_files(
            client_id=getattr(ima_cfg, "client_id", None),
            api_key=getattr(ima_cfg, "api_key", None),
            base_url=getattr(ima_cfg, "base_url", "https://ima.qq.com"),
        )
        if not client.has_credentials():
            logger.warning("knowledge_sync: IMA credentials missing, skipping")
            return

        import json as _json, re as _re, httpx as _httpx

        vault_path = getattr(obs_cfg, "vault_path", None) or str(config.workspace_path)
        vault = Path(vault_path).expanduser().resolve()
        inbox = vault / "Nanobot" / "Inbox"
        inbox.mkdir(parents=True, exist_ok=True)

        headers = {
            "ima-openapi-clientid": client.client_id,
            "ima-openapi-apikey": client.api_key,
            "Content-Type": "application/json",
        }
        base = getattr(ima_cfg, "base_url", "https://ima.qq.com")

        # ── cursor: track processed media_ids ──────────────────────────
        cursor_file = Path(config.workspace_path) / "ima" / ".processed_ids.json"
        cursor_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            processed_ids: set[str] = set(_json.loads(cursor_file.read_text(encoding="utf-8")))
        except Exception:
            processed_ids = set()

        # ── list knowledge bases ────────────────────────────────────────
        r = _httpx.post(
            f"{base}/openapi/wiki/v1/search_knowledge_base",
            json={"query": "", "cursor": "", "limit": 20},
            headers=headers,
            timeout=15,
        )
        data = r.json()
        created = 0
        skipped = 0
        if data.get("code") != 0:
            logger.warning("knowledge_sync: IMA list KB failed: {}", data.get("msg"))
            return

        kbs = data.get("data", {}).get("info_list") or []
        for kb in kbs:
            kb_id = kb.get("kb_id", "")
            kb_name = kb.get("kb_name", "")
            kr = _httpx.post(
                f"{base}/openapi/wiki/v1/get_knowledge_list",
                json={"knowledge_base_id": kb_id, "cursor": "", "limit": 50},
                headers=headers,
                timeout=15,
            )
            kd = kr.json()
            if kd.get("code") != 0:
                continue
            for item in (kd.get("data", {}).get("list") or [])[:10]:
                media_id = item.get("media_id", "")
                title = item.get("title", item.get("name", "Untitled"))
                if not media_id or media_id in processed_ids:
                    skipped += 1
                    continue
                slug = _re.sub(r"[^a-z0-9-]", "", title.lower().strip().replace(" ", "-"))
                slug = (slug or f"ima-{hash(title) % 10000}")[:80]

                content = await _fetch_content(base, headers, media_id)

                fc = (
                    f"---\ntitle: \"{title}\"\nsource: ima:{media_id}\n"
                    f"kb: {kb_name}\ncaptured_at: {datetime.now(timezone.utc).isoformat()}\n---\n\n"
                    f"{content[:8000]}"
                )
                file_path = inbox / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{slug}.md"
                file_path.write_text(fc, encoding="utf-8")
                processed_ids.add(media_id)
                created += 1

        # ── persist cursor ─────────────────────────────────────────────
        cursor_file = Path(config.workspace_path) / "ima" / ".processed_ids.json"
        cursor_file.parent.mkdir(parents=True, exist_ok=True)
        cursor_file.write_text(_json.dumps(list(processed_ids)), encoding="utf-8")
        logger.info("knowledge_sync: IMA synced {} new, {} already done", created, skipped)

    except Exception as exc:
        logger.warning("knowledge_sync: IMA sync failed: {}", exc)


async def _fetch_content(base: str, headers: dict, media_id: str) -> str:
    """Fetch content for an IMA media item (note body or URL)."""
    import re as _re
    import httpx as _httpx

    content = ""
    # Try get_doc_content first (for note-type items)
    try:
        mr = _httpx.post(
            f"{base}/openapi/note/v1/get_doc_content",
            json={"note_id": media_id, "target_content_format": 0},
            headers=headers,
            timeout=15,
        )
        md = mr.json()
        if md.get("code") == 0:
            ddata = md.get("data", {})
            content = ddata.get("content") or ddata.get("doc_content") or ddata.get("text") or ""
    except Exception:
        pass

    # If no content, try get_media_info → fetch URL
    if not content:
        try:
            ir = _httpx.post(
                f"{base}/openapi/wiki/v1/get_media_info",
                json={"media_id": media_id},
                headers=headers,
                timeout=15,
            )
            idata = ir.json()
            if idata.get("code") == 0:
                url = (idata.get("data", {}).get("url_info") or {}).get("url", "")
                if url:
                    resp = _httpx.get(url, timeout=15, follow_redirects=True)
                    if resp.status_code == 200:
                        content = _re.sub(r"<[^>]+>", " ", resp.text)[:8000]
        except Exception:
            pass

    return content


async def _sync_obsidian_to_wiki(config: Any, obs_cfg: Any, _Path: type) -> None:
    """Scan Obsidian vault → create wiki pages."""
    try:
        vault_path = Path(obs_cfg.vault_path).expanduser().resolve()
        if not vault_path.exists():
            logger.warning("knowledge_sync: Obsidian vault not found: {}", vault_path)
            return

        from nanobot.agent.wiki import WikiPaths, WikiStore
        from nanobot.agent.wiki.frontmatter import parse_frontmatter

        import hashlib
        import re as _re

        workspace = config.workspace_path
        paths = WikiPaths.from_workspace(Path(workspace))
        store = WikiStore(paths)
        md_files = sorted(vault_path.rglob("*.md"))
        created = 0
        for md_file in md_files[:30]:
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if not content.strip():
                continue

            fm, body = parse_frontmatter(content)
            title = fm.title or md_file.stem
            stem = md_file.stem
            slug = _re.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem).lower().replace(" ", "-")
            slug = _re.sub(r"[^a-z0-9-]", "", slug).strip("-")
            if not slug or len(slug) < 3:
                slug = f"vault-{hashlib.md5(md_file.name.encode()).hexdigest()[:8]}"
            slug = slug[:80]

            body_content = body if body else content
            try:
                store.write_page(
                    slug=slug,
                    title=title,
                    body=body_content[:8000],
                    tags=["obsidian", "vault"],
                    source=f"obsidian:{md_file.relative_to(vault_path).as_posix()}",
                )
                created += 1
            except Exception:
                pass
        logger.info("knowledge_sync: Obsidian synced {} pages to wiki", created)
    except Exception as exc:
        logger.warning("knowledge_sync: Obsidian sync failed: {}", exc)