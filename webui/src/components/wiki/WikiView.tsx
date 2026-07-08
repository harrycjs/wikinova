import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  BookOpen,
  RefreshCw,
  Search,
  Trash2,
  SquareCheck,
  Square,
  Pencil,
  X,
  Save,
  Upload,
  Link as LinkIcon,
} from "lucide-react";

import { api } from "@/lib/api-client";
import { MarkdownText } from "@/components/MarkdownText";
import { cn } from "@/lib/utils";

interface WikiPageSummary {
  slug: string;
  title: string;
  tags?: string[];
  mtime?: string;
  sha?: string;
}

interface WikiPageDetail extends WikiPageSummary {
  body: string;
  frontmatter?: Record<string, unknown>;
  backlinks?: string[];
  // When the on-disk byte count exceeds the rendered body length, the page
  // was clipped by ``WikiConfig.max_page_chars``. The UI surfaces a warning
  // and offers a "View full" link to open the original markdown source.
  stored_length?: number;
}

interface EvolutionEntry {
  ran: boolean;
  cursor_before: number;
  cursor_after: number;
  pages_changed: string[];
  summary: string;
  started_at: string;
  finished_at: string;
}

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

export function WikiView(): JSX.Element {
  const { t } = useTranslation();
  const [pages, setPages] = useState<WikiPageSummary[]>([]);
  const [query, setQuery] = useState("");
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [detail, setDetail] = useState<WikiPageDetail | null>(null);
  const [evolution, setEvolution] = useState<EvolutionEntry[]>([]);
  const [activeTab, setActiveTab] = useState<"browse" | "evolution">("browse");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedSlugs, setSelectedSlugs] = useState<Set<string>>(new Set());
  // Edit-mode state. ``editingSlug === detail.slug`` while the user has the
  // editor open. Drafts are stored separately from ``detail`` so toggling
  // Edit does not mutate the loaded page until Save is pressed.
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftBody, setDraftBody] = useState("");
  const [draftTags, setDraftTags] = useState("");
  const [draftPreview, setDraftPreview] = useState(false);

  // Import-mode state. ``importOpen`` toggles the modal; ``importUrl`` is the
  // URL the user wants to fetch. File uploads use a hidden input ref so the
  // modal layout stays simple.
  const [importOpen, setImportOpen] = useState(false);
  const [importUrl, setImportUrl] = useState("");
  const [importing, setImporting] = useState(false);
  const [importFeedback, setImportFeedback] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const debouncedQuery = useDebounced(query, 300);
  const searchHits = useMemo(() => {
    if (!debouncedQuery.trim()) return null;
    return pages.filter((p) => {
      const haystack = `${p.title} ${(p.tags || []).join(" ")}`.toLowerCase();
      return haystack.includes(debouncedQuery.toLowerCase());
    });
  }, [debouncedQuery, pages]);

  const visiblePages = searchHits ?? pages;

  async function refresh(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const [list, log] = await Promise.all([
        api.get<WikiPageSummary[]>("/api/wiki/list"),
        api.get<EvolutionEntry[]>("/api/wiki/evolution"),
      ]);
      setPages(list);
      setEvolution(log);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function loadDetail(slug: string): Promise<void> {
    setSelectedSlug(slug);
    try {
      const data = await api.get<WikiPageDetail>(`/api/wiki/page?slug=${encodeURIComponent(slug)}`);
      setDetail(data);
    } catch (err) {
      setDetail(null);
      setError((err as Error).message);
    }
  }

  async function triggerEvolution(): Promise<void> {
    setBusy(true);
    try {
      await api.get("/api/wiki/regenerate");
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function toggleSelect(slug: string): void {
    setSelectedSlugs((prev) => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });
  }

  function toggleSelectAll(): void {
    if (selectedSlugs.size === visiblePages.length) {
      setSelectedSlugs(new Set());
    } else {
      setSelectedSlugs(new Set(visiblePages.map((p) => p.slug)));
    }
  }

  async function deleteSelected(): Promise<void> {
    if (selectedSlugs.size === 0) return;
    if (!confirm(`Delete ${selectedSlugs.size} page(s)?`)) return;
    setBusy(true);
    setError(null);
    try {
      const slugs = Array.from(selectedSlugs);
      await Promise.all(slugs.map((s) => api.get(`/api/wiki/delete?slug=${encodeURIComponent(s)}`)));
      if (selectedSlug && selectedSlugs.has(selectedSlug)) {
        setSelectedSlug(null);
        setDetail(null);
      }
      setSelectedSlugs(new Set());
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function deletePage(slug: string): Promise<void> {
    if (!confirm(t("wiki.delete.confirm", { slug }))) return;
    setBusy(true);
    try {
      await api.get(`/api/wiki/delete?slug=${encodeURIComponent(slug)}`);
      if (selectedSlug === slug) {
        setSelectedSlug(null);
        setDetail(null);
      }
      setSelectedSlugs((prev) => {
        const next = new Set(prev);
        next.delete(slug);
        return next;
      });
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function enterEditMode(): void {
    if (!detail) return;
    setEditingSlug(detail.slug);
    setDraftTitle(detail.title);
    setDraftBody(detail.body);
    const rawTags = detail.frontmatter?.tags;
    const tagList = Array.isArray(rawTags)
      ? rawTags.filter((t): t is string => typeof t === "string")
      : [];
    setDraftTags(tagList.join(", "));
    setDraftPreview(false);
    setError(null);
  }

  function cancelEdit(): void {
    setEditingSlug(null);
    setDraftTitle("");
    setDraftBody("");
    setDraftTags("");
    setDraftPreview(false);
  }

  async function saveEdit(): Promise<void> {
    if (!editingSlug) return;
    const title = draftTitle.trim() || editingSlug;
    const tags = draftTags
      .split(/[,\n]/)
      .map((tag) => tag.trim())
      .filter(Boolean);
    setBusy(true);
    setError(null);
    try {
      const result = await api.post<{ ok: boolean; page: WikiPageSummary }>(
        "/api/wiki/edit",
        { slug: editingSlug, title, body: draftBody, tags },
      );
      // Re-load the page so the view reflects the saved version and refresh
      // the list (mtime + title may have changed).
      await loadDetail(editingSlug);
      await refresh();
      setEditingSlug(null);
      void result;
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function importFiles(files: FileList | null): Promise<void> {
    if (!files || files.length === 0) return;
    setImporting(true);
    setImportFeedback(null);
    const successes: string[] = [];
    const failures: string[] = [];
    for (const file of Array.from(files)) {
      try {
        const form = new FormData();
        form.append("file", file);
        const result = await api.post<{ ok: boolean; slug: string; title: string }>(
          "/api/wiki/import",
          form,
        );
        successes.push(result.title || result.slug);
      } catch (err) {
        failures.push(`${file.name}: ${(err as Error).message}`);
      }
    }
    setImporting(false);
    setImportFeedback(
      successes.length > 0
        ? `Imported ${successes.length} file(s)${failures.length ? `; ${failures.length} failed` : ""}`
        : `Import failed: ${failures.join("; ")}`,
    );
    await refresh();
  }

  async function submitImportUrl(): Promise<void> {
    if (!importUrl.trim()) return;
    setImporting(true);
    setImportFeedback(null);
    try {
      const result = await api.post<{ ok: boolean; slug: string; title: string }>(
        "/api/wiki/import-url",
        { type: "url", url: importUrl.trim() },
      );
      setImportFeedback(`Imported "${result.title || result.slug}"`);
      setImportUrl("");
      await refresh();
    } catch (err) {
      setImportFeedback(`Import failed: ${(err as Error).message}`);
    } finally {
      setImporting(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div className="flex h-full flex-col jobs-mode">
      <header className="flex items-center justify-between border-b border-border/40 px-6 py-4">
        <div className="flex items-center gap-3">
          <BookOpen className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-medium">{t("wiki.title")}</h1>
          {selectedSlugs.size > 0 && (
            <span className="text-sm text-muted-foreground">({selectedSlugs.size} selected)</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {selectedSlugs.size > 0 && (
            <button
              type="button"
              onClick={deleteSelected}
              disabled={busy}
              className="flex items-center gap-1.5 rounded border border-destructive/40 px-3 py-1.5 text-sm text-destructive hover:bg-destructive/10 disabled:opacity-50"
            >
              <Trash2 className="h-3.5 w-3.5" />
              Delete ({selectedSlugs.size})
            </button>
          )}
          <button
            type="button"
            onClick={() => setImportOpen(true)}
            disabled={busy}
            className="flex items-center gap-1.5 rounded border border-border/60 px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
            data-testid="wiki-import-open"
          >
            <Upload className="h-3.5 w-3.5" />
            {t("wiki.import.button", { defaultValue: "Import" })}
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("browse")}
            className={cn(
              "px-3 py-1.5 text-sm",
              activeTab === "browse" ? "font-medium text-foreground" : "text-muted-foreground",
            )}
          >
            {t("wiki.browse.tab")}
          </button>
          <button
            type="button"
            onClick={() => setActiveTab("evolution")}
            className={cn(
              "px-3 py-1.5 text-sm",
              activeTab === "evolution" ? "font-medium text-foreground" : "text-muted-foreground",
            )}
          >
            {t("wiki.evolution.title")}
          </button>
          <button
            type="button"
            onClick={triggerEvolution}
            disabled={busy}
            className="ml-3 flex items-center gap-1.5 rounded border border-border px-3 py-1.5 text-sm hover:bg-muted/40 disabled:opacity-50"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", busy && "animate-spin")} />
            {t("wiki.evolution.runNow")}
          </button>
        </div>
      </header>

      {error && (
        <div className="border-b border-destructive/30 bg-destructive/5 px-6 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {activeTab === "browse" ? (
        <div className="grid flex-1 h-full grid-cols-[280px_minmax(0,1fr)_240px] divide-x divide-border/40">
          {/* Left: page list */}
          <aside className="flex flex-col h-full overflow-hidden">
            <div className="border-b border-border/40 p-3">
              <div className="flex items-center gap-2 rounded border border-border/60 bg-background px-2.5 py-1.5">
                <Search className="h-3.5 w-3.5 text-muted-foreground" />
                <input
                  type="text"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder={t("wiki.search.placeholder")}
                  className="flex-1 bg-transparent text-sm outline-none"
                />
              </div>
              {visiblePages.length > 0 && (
                <button
                  type="button"
                  onClick={toggleSelectAll}
                  className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
                >
                  {selectedSlugs.size === visiblePages.length ? (
                    <SquareCheck className="h-3 w-3" />
                  ) : (
                    <Square className="h-3 w-3" />
                  )}
                  {selectedSlugs.size === visiblePages.length ? "Deselect all" : "Select all"}
                </button>
              )}
            </div>
            <ul className="flex-1 min-h-0 overflow-y-auto">
              {visiblePages.map((page) => (
                <li key={page.slug} className="group flex items-center">
                  <button
                    type="button"
                    onClick={(e) => {
                      if (e.shiftKey || e.ctrlKey || e.metaKey) {
                        toggleSelect(page.slug);
                      } else {
                        loadDetail(page.slug);
                      }
                    }}
                    className={cn(
                      "flex-1 px-3 py-2 text-left text-sm",
                      selectedSlug === page.slug
                        ? "bg-muted/60 text-foreground"
                        : "hover:bg-muted/30 text-muted-foreground",
                    )}
                  >
                    <div className="font-medium text-foreground">{page.title || page.slug}</div>
                    <div className="text-xs text-muted-foreground/80">{page.slug}</div>
                  </button>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleSelect(page.slug);
                    }}
                    className="mr-1 hidden px-1 py-1 text-muted-foreground hover:text-destructive group-hover:block"
                    title="Select for delete"
                  >
                    {selectedSlugs.has(page.slug) ? (
                      <SquareCheck className="h-3.5 w-3.5 text-foreground" />
                    ) : (
                      <Square className="h-3.5 w-3.5" />
                    )}
                  </button>
                </li>
              ))}
              {visiblePages.length === 0 && (
                <li className="px-3 py-6 text-center text-xs text-muted-foreground">
                  {t("wiki.list.empty")}
                </li>
              )}
            </ul>
          </aside>

          {/* Center: page detail */}
          <main className="overflow-y-auto px-8 py-6">
            {detail && editingSlug === detail.slug ? (
              <EditPanel
                title={draftTitle}
                body={draftBody}
                tags={draftTags}
                preview={draftPreview}
                onTitleChange={setDraftTitle}
                onBodyChange={setDraftBody}
                onTagsChange={setDraftTags}
                onTogglePreview={() => setDraftPreview((v) => !v)}
                onCancel={cancelEdit}
                onSave={saveEdit}
                saving={busy}
              />
            ) : detail ? (
              <article className="prose prose-sm max-w-none dark:prose-invert">
                <header className="mb-6 flex items-start justify-between border-b border-border/40 pb-3">
                  <div>
                    <h1 className="!mb-1 text-2xl font-medium">{detail.title}</h1>
                    <div className="text-xs text-muted-foreground">
                      {detail.mtime && t("wiki.page.updatedAt", { date: detail.mtime })}
                    </div>
                  </div>
                  <div className="flex items-center gap-1">
                    <button
                      type="button"
                      onClick={enterEditMode}
                      className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                      title={t("wiki.edit.open", { defaultValue: "Edit page" })}
                      aria-label={t("wiki.edit.open", { defaultValue: "Edit page" })}
                      data-testid="wiki-edit-button"
                    >
                      <Pencil className="h-4 w-4" />
                    </button>
                    <button
                      type="button"
                      onClick={() => deletePage(detail.slug)}
                      className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                      title={t("wiki.delete.button", { defaultValue: "Delete page" })}
                      aria-label={t("wiki.delete.button", { defaultValue: "Delete page" })}
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </header>
                {detail.stored_length != null &&
                detail.stored_length > detail.body.length + 500 ? (
                  <div
                    className="mb-3 inline-flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-xs text-amber-700 dark:border-amber-300/30 dark:bg-amber-300/10 dark:text-amber-200"
                    data-testid="wiki-truncation-warning"
                  >
                    <span className="font-medium">
                      {t("wiki.truncated.label", {
                        defaultValue: "Body truncated on write",
                      })}
                    </span>
                    <span className="text-amber-700/80 dark:text-amber-200/80">
                      {t("wiki.truncated.detail", {
                        stored: detail.stored_length,
                        shown: detail.body.length,
                        defaultValue:
                          "stored {{stored}} bytes, showing first {{shown}} — bump WikiConfig.max_page_chars to keep full body",
                      })}
                    </span>
                  </div>
                ) : null}
                <pre className="whitespace-pre-wrap text-sm leading-relaxed">{detail.body}</pre>
              </article>
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                {t("wiki.empty.detail")}
              </div>
            )}
          </main>

          {/* Right: backlinks + tags */}
          <aside className="overflow-y-auto p-4 text-sm">
            {detail?.backlinks && detail.backlinks.length > 0 && (
              <section className="mb-6">
                <h3 className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">
                  {t("wiki.page.backlinks")}
                </h3>
                <ul className="space-y-1">
                  {detail.backlinks.map((slug) => (
                    <li key={slug}>
                      <button
                        type="button"
                        onClick={() => loadDetail(slug)}
                        className="text-left text-foreground/80 hover:text-foreground"
                      >
                        {slug}
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {detail?.tags && detail.tags.length > 0 && (
              <section>
                <h3 className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">
                  {t("wiki.page.tags")}
                </h3>
                <div className="flex flex-wrap gap-1.5">
                  {detail.tags.map((tag) => (
                    <span
                      key={tag}
                      className="rounded-full border border-border/60 px-2 py-0.5 text-xs text-muted-foreground"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              </section>
            )}
          </aside>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto px-6 py-4">
          <h2 className="mb-3 text-sm uppercase tracking-wide text-muted-foreground">
            {t("wiki.evolution.title")}
          </h2>
          {evolution.length === 0 ? (
            <div className="text-sm text-muted-foreground">{t("wiki.evolution.empty")}</div>
          ) : (
            <ul className="divide-y divide-border/40">
              {evolution
                .slice()
                .reverse()
                .map((entry, i) => (
                  <li key={i} className="py-3 text-sm">
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-xs text-muted-foreground">
                        {entry.started_at}
                      </span>
                      <span className="text-xs text-muted-foreground">
                        {entry.cursor_before} → {entry.cursor_after}
                      </span>
                    </div>
                    <div className="mt-1 text-foreground">{entry.summary}</div>
                    {entry.pages_changed.length > 0 && (
                      <div className="mt-1 text-xs text-muted-foreground">
                        {entry.pages_changed.join(", ")}
                      </div>
                    )}
                  </li>
                ))}
            </ul>
          )}
        </div>
      )}
      <ImportModal
        open={importOpen}
        url={importUrl}
        feedback={importFeedback}
        importing={importing}
        onUrlChange={setImportUrl}
        onUrlSubmit={submitImportUrl}
        onClose={() => {
          setImportOpen(false);
          setImportFeedback(null);
        }}
        onFiles={importFiles}
        fileInputRef={fileInputRef}
      />
    </div>
  );
}

interface EditPanelProps {
  title: string;
  body: string;
  tags: string;
  preview: boolean;
  saving: boolean;
  onTitleChange: (next: string) => void;
  onBodyChange: (next: string) => void;
  onTagsChange: (next: string) => void;
  onTogglePreview: () => void;
  onCancel: () => void;
  onSave: () => void;
}

/**
 * Markdown editor for an existing wiki page. Renders a title input, a tag
 * chip-list input, and either a textarea + preview tab or a rendered preview
 * pane. The Save / Cancel buttons mirror the rest of the UI's button style.
 *
 * Kept intentionally simple — a textarea + markdown preview covers the common
 * case without pulling in CodeMirror or Monaco, which would inflate the
 * initial bundle for an admin-facing view.
 */
function EditPanel({
  title,
  body,
  tags,
  preview,
  saving,
  onTitleChange,
  onBodyChange,
  onTagsChange,
  onTogglePreview,
  onCancel,
  onSave,
}: EditPanelProps): JSX.Element {
  return (
    <div className="flex flex-col gap-3" data-testid="wiki-edit-panel">
      <div className="flex items-center justify-between border-b border-border/40 pb-2">
        <h2 className="text-sm font-medium text-muted-foreground">
          Editing page
        </h2>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={onTogglePreview}
            className={cn(
              "rounded px-2 py-1 text-xs font-medium",
              preview
                ? "bg-foreground/10 text-foreground"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
            data-testid="wiki-edit-toggle-preview"
          >
            {preview ? "Edit" : "Preview"}
          </button>
          <button
            type="button"
            onClick={onCancel}
            disabled={saving}
            className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
          >
            <X className="h-3.5 w-3.5" />
            Cancel
          </button>
          <button
            type="button"
            onClick={onSave}
            disabled={saving}
            className="inline-flex items-center gap-1 rounded bg-primary px-3 py-1 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            data-testid="wiki-edit-save"
          >
            <Save className="h-3.5 w-3.5" />
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
      <label className="flex flex-col gap-1 text-xs text-muted-foreground">
        Title
        <input
          type="text"
          value={title}
          onChange={(e) => onTitleChange(e.target.value)}
          className="rounded border border-border/60 bg-background px-2.5 py-1.5 text-sm text-foreground outline-none focus:border-primary"
          data-testid="wiki-edit-title"
        />
      </label>
      <label className="flex flex-col gap-1 text-xs text-muted-foreground">
        Tags (comma-separated)
        <input
          type="text"
          value={tags}
          onChange={(e) => onTagsChange(e.target.value)}
          placeholder="AI, 笔记, 项目"
          className="rounded border border-border/60 bg-background px-2.5 py-1.5 text-sm text-foreground outline-none focus:border-primary"
          data-testid="wiki-edit-tags"
        />
      </label>
      {preview ? (
        <div className="min-h-[24rem] rounded border border-border/60 bg-background p-4">
          <MarkdownText streaming={false}>{body}</MarkdownText>
        </div>
      ) : (
        <textarea
          value={body}
          onChange={(e) => onBodyChange(e.target.value)}
          className="min-h-[24rem] resize-y rounded border border-border/60 bg-background p-3 font-mono text-sm leading-relaxed text-foreground outline-none focus:border-primary"
          data-testid="wiki-edit-body"
        />
      )}
    </div>
  );
}

interface ImportModalProps {
  open: boolean;
  url: string;
  feedback: string | null;
  importing: boolean;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  onUrlChange: (next: string) => void;
  onUrlSubmit: () => void;
  onClose: () => void;
  onFiles: (files: FileList | null) => void;
}

/**
 * Modal dialog for importing files (drag-drop or file picker) or a remote URL
 * into the wiki. Calls ``POST /api/wiki/import`` per source and surfaces a
 * success / failure banner so users can recover from individual failures.
 */
function ImportModal({
  open,
  url,
  feedback,
  importing,
  fileInputRef,
  onUrlChange,
  onUrlSubmit,
  onClose,
  onFiles,
}: ImportModalProps): JSX.Element | null {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-background/70 p-4"
      data-testid="wiki-import-modal"
      role="dialog"
      aria-modal="true"
    >
      <div className="w-full max-w-md rounded-lg border border-border/70 bg-background p-5 shadow-xl">
        <header className="mb-4 flex items-center justify-between">
          <h2 className="text-base font-semibold">Import into wiki</h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div
          onDragOver={(e) => {
            e.preventDefault();
          }}
          onDrop={(e) => {
            e.preventDefault();
            onFiles(e.dataTransfer.files);
          }}
          className="mb-4 flex flex-col items-center justify-center gap-2 rounded-md border-2 border-dashed border-border/60 bg-muted/30 p-6 text-center text-sm text-muted-foreground"
        >
          <Upload className="h-6 w-6" />
          <p>Drag &amp; drop .md / .txt / .pdf files here</p>
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={importing}
            className="rounded border border-border/60 px-3 py-1 text-xs hover:bg-muted disabled:opacity-50"
          >
            Choose files
          </button>
          <input
            ref={fileInputRef as React.Ref<HTMLInputElement>}
            type="file"
            multiple
            accept=".md,.markdown,.txt,.pdf,text/markdown,text/plain,application/pdf"
            className="hidden"
            onChange={(e) => onFiles(e.target.files)}
            data-testid="wiki-import-file-input"
          />
        </div>

        <div className="mb-3 flex items-center gap-2 text-xs uppercase tracking-wide text-muted-foreground">
          <span className="flex-1 border-t border-border/40" />
          <span>or import URL</span>
          <span className="flex-1 border-t border-border/40" />
        </div>
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            onUrlSubmit();
          }}
        >
          <LinkIcon className="h-4 w-4 text-muted-foreground" />
          <input
            type="url"
            value={url}
            onChange={(e) => onUrlChange(e.target.value)}
            placeholder="https://example.com/article"
            disabled={importing}
            className="flex-1 rounded border border-border/60 bg-background px-2.5 py-1.5 text-sm outline-none focus:border-primary"
            data-testid="wiki-import-url-input"
          />
          <button
            type="submit"
            disabled={importing || !url.trim()}
            className="rounded bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            data-testid="wiki-import-url-submit"
          >
            {importing ? "Importing…" : "Fetch"}
          </button>
        </form>

        {feedback ? (
          <div
            className={cn(
              "mt-3 rounded-md border px-3 py-2 text-xs",
              feedback.startsWith("Import failed")
                ? "border-destructive/40 bg-destructive/10 text-destructive"
                : "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-200",
            )}
            data-testid="wiki-import-feedback"
          >
            {feedback}
          </div>
        ) : null}
      </div>
    </div>
  );
}
