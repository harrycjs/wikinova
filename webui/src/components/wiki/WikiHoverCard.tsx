import {
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactElement,
} from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";

import { api } from "@/lib/api-client";

interface WikiHoverCardProps {
  slug: string;
  children: ReactElement;
}

interface WikiPageSummary {
  title?: string;
  body?: string;
  frontmatter?: {
    title?: string;
    summary?: string;
    tags?: string[];
    [key: string]: unknown;
  };
}

const HOVER_OPEN_DELAY_MS = 250;
const HOVER_CLOSE_DELAY_MS = 120;
const SUMMARY_PREVIEW_CHARS = 320;
const VIEWPORT_PADDING = 8;

interface CardPosition {
  top: number;
  left: number;
}

/**
 * Inline hover-card for ``[wiki:slug]`` citations. Fetches the wiki page
 * summary on first open and caches for the lifetime of the component. Uses
 * a plain ``mouseenter`` / ``mouseleave`` state machine with open/close
 * delays so accidental hovers don't trigger network calls.
 */
export function WikiHoverCard({ slug, children }: WikiHoverCardProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<WikiPageSummary | null>(null);
  const [loadState, setLoadState] = useState<"idle" | "loading" | "loaded" | "error">(
    "idle",
  );
  const [position, setPosition] = useState<CardPosition | null>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const openTimerRef = useRef<number | null>(null);
  const closeTimerRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const clearTimer = (ref: React.MutableRefObject<number | null>) => {
    if (ref.current !== null) {
      window.clearTimeout(ref.current);
      ref.current = null;
    }
  };

  const cancelFetch = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
  }, []);

  const computePosition = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const cardWidth = 320; // matches `w-80`
    const cardHeight = 160;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;

    let top = rect.top - cardHeight - 8;
    if (top < VIEWPORT_PADDING) {
      // Flip below the trigger if there's no room above.
      top = rect.bottom + 8;
    }
    top = Math.min(
      Math.max(top, VIEWPORT_PADDING),
      viewportHeight - cardHeight - VIEWPORT_PADDING,
    );

    let left = rect.left;
    if (left + cardWidth > viewportWidth - VIEWPORT_PADDING) {
      left = viewportWidth - cardWidth - VIEWPORT_PADDING;
    }
    left = Math.max(left, VIEWPORT_PADDING);

    setPosition({ top, left });
  }, []);

  const handleEnter = useCallback(() => {
    clearTimer(closeTimerRef);
    if (open) return;
    openTimerRef.current = window.setTimeout(() => {
      openTimerRef.current = null;
      setOpen(true);
    }, HOVER_OPEN_DELAY_MS);
  }, [open]);

  const handleLeave = useCallback(() => {
    clearTimer(openTimerRef);
    closeTimerRef.current = window.setTimeout(() => {
      closeTimerRef.current = null;
      setOpen(false);
      cancelFetch();
    }, HOVER_CLOSE_DELAY_MS);
  }, [cancelFetch]);

  useEffect(() => {
    if (!open) {
      setPage(null);
      setLoadState("idle");
      setPosition(null);
      return;
    }
    computePosition();
    setLoadState("loading");
    cancelFetch();
    const controller = new AbortController();
    abortRef.current = controller;
    api
      .get<WikiPageSummary>(`/api/wiki/page?slug=${encodeURIComponent(slug)}`)
      .then((data) => {
        if (controller.signal.aborted) return;
        setPage(data);
        setLoadState("loaded");
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        const status =
          err && typeof err === "object" && "status" in err
            ? (err as { status?: number }).status
            : undefined;
        if (status !== 404 && status !== 401) {
          // eslint-disable-next-line no-console
          console.warn("WikiHoverCard fetch failed", err);
        }
        setLoadState("error");
      });
    return () => cancelFetch();
  }, [cancelFetch, computePosition, open, slug]);

  useEffect(() => {
    if (!open) return;
    const onScrollOrResize = () => computePosition();
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
    return () => {
      window.removeEventListener("scroll", onScrollOrResize, true);
      window.removeEventListener("resize", onScrollOrResize);
    };
  }, [computePosition, open]);

  useEffect(() => {
    return () => {
      clearTimer(openTimerRef);
      clearTimer(closeTimerRef);
      cancelFetch();
    };
  }, [cancelFetch]);

  // Clone the trigger element so we can attach event handlers and a ref
  // without forcing the caller to forward one.
  const trigger = isValidElement(children)
    ? cloneElement(children as ReactElement<{
        ref?: React.Ref<HTMLElement>;
        onMouseEnter?: (e: React.MouseEvent) => void;
        onMouseLeave?: (e: React.MouseEvent) => void;
        onFocus?: (e: React.FocusEvent) => void;
        onBlur?: (e: React.FocusEvent) => void;
      }>, {
        ref: (node: HTMLElement | null) => {
          triggerRef.current = node;
        },
        onMouseEnter: handleEnter,
        onMouseLeave: handleLeave,
        onFocus: handleEnter,
        onBlur: handleLeave,
      })
    : children;

  return (
    <>
      {trigger}
      {open && position
        ? createPortal(
            <div
              data-testid="wiki-hover-card"
              onMouseEnter={handleEnter}
              onMouseLeave={handleLeave}
              style={{
                position: "fixed",
                top: position.top,
                left: position.left,
                width: 320,
                zIndex: 50,
              }}
              className="rounded-lg border border-border/70 bg-popover p-3 text-popover-foreground shadow-lg"
            >
              <HoverCardContent slug={slug} page={page} loadState={loadState} t={t} />
            </div>,
            document.body,
          )
        : null}
    </>
  );
}

function HoverCardContent({
  slug,
  page,
  loadState,
  t,
}: {
  slug: string;
  page: WikiPageSummary | null;
  loadState: "idle" | "loading" | "loaded" | "error";
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  const title = page?.frontmatter?.title || page?.title || slug;
  const rawSummary =
    page?.frontmatter?.summary || (typeof page?.body === "string" ? page.body : "");
  const preview = rawSummary ? rawSummary.slice(0, SUMMARY_PREVIEW_CHARS).trim() : "";
  const tags = Array.isArray(page?.frontmatter?.tags) ? page.frontmatter.tags : [];

  return (
    <div className="flex flex-col gap-1.5 text-left">
      <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {t("wiki.citation.sourceLabel", { defaultValue: "Wiki source" })}
      </div>
      <div className="text-sm font-semibold leading-snug text-foreground/95">{title}</div>
      {loadState === "loading" ? (
        <div className="text-xs text-muted-foreground">
          {t("common.loading", { defaultValue: "Loading…" })}
        </div>
      ) : null}
      {loadState === "error" ? (
        <div className="text-xs text-amber-600 dark:text-amber-300">
          {t("wiki.citation.pageMissing", {
            slug,
            defaultValue: `Page "${slug}" not found in wiki`,
          })}
        </div>
      ) : null}
      {loadState === "loaded" && preview ? (
        <div className="text-xs leading-relaxed text-foreground/80">
          {preview}
          {rawSummary.length > SUMMARY_PREVIEW_CHARS ? "…" : ""}
        </div>
      ) : null}
      {tags.length > 0 ? (
        <div className="flex flex-wrap gap-1 pt-1">
          {tags.slice(0, 4).map((tag) => (
            <span
              key={tag}
              className="rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground"
            >
              #{tag}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Render a ``[wiki:slug]`` reference as a clickable inline badge.
 *
 * - Shows the slug as the visible label so users can see what's being cited.
 * - Wraps the badge in ``WikiHoverCard`` for an at-a-glance preview.
 * - Clicking jumps to the wiki page (front-end route ``/wiki?slug=…``).
 */
export function WikiCitationToken({ slug }: { slug: string }) {
  return (
    <WikiHoverCard slug={slug}>
      <a
        href={`/wiki?slug=${encodeURIComponent(slug)}`}
        data-testid="wiki-citation-badge"
        data-wiki-slug={slug}
        className="mx-0.5 inline-flex cursor-pointer items-center gap-1 rounded-md border border-blue-500/30 bg-blue-500/10 px-1.5 py-0.5 align-baseline font-mono text-[0.78em] text-blue-700 no-underline transition-colors hover:bg-blue-500/20 dark:border-blue-300/30 dark:bg-blue-300/10 dark:text-blue-200 dark:hover:bg-blue-300/20"
        onClick={(e) => {
          // Allow ctrl/cmd-click to open in a new tab; otherwise let the
          // SPA router (or fallback full-page nav) handle the transition.
          if (e.ctrlKey || e.metaKey) return;
        }}
      >
        <span aria-hidden className="opacity-70">
          wiki:
        </span>
        <span className="font-semibold">{slug}</span>
      </a>
    </WikiHoverCard>
  );
}