import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { BookOpen, RefreshCw, Search, Trash2, SquareCheck, Square } from "lucide-react";

import { api } from "@/lib/api-client";
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
        <div className="grid flex-1 grid-cols-[280px_minmax(0,1fr)_240px] divide-x divide-border/40">
          {/* Left: page list */}
          <aside className="flex flex-col">
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
            <ul className="flex-1 overflow-y-auto">
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
            {detail ? (
              <article className="prose prose-sm max-w-none dark:prose-invert">
                <header className="mb-6 flex items-start justify-between border-b border-border/40 pb-3">
                  <div>
                    <h1 className="!mb-1 text-2xl font-medium">{detail.title}</h1>
                    <div className="text-xs text-muted-foreground">
                      {detail.mtime && t("wiki.page.updatedAt", { date: detail.mtime })}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => deletePage(detail.slug)}
                    className="text-muted-foreground hover:text-destructive"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </header>
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
    </div>
  );
}
