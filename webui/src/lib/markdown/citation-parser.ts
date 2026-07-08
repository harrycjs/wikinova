/**
 * Parse ``[wiki:slug-name]`` citation tokens out of a plain-text string.
 *
 * The agent's knowledge-qa skill mandates that every factual statement be
 * followed by a ``[wiki:slug]`` marker (see ``nanobot/skills/knowledge-qa/SKILL.md``).
 * Up to now these tokens have been rendered as raw text. This module extracts
 * them so the chat UI can render each one as a clickable badge with a hover
 * preview card.
 *
 * Slug rules (mirrored from ``WikiStore`` and the agent-side regex):
 *   - First char is a lowercase ASCII letter.
 *   - Subsequent chars are lowercase ASCII letters, digits, or hyphens.
 *   - Maximum 96 chars total.
 */

export type CitationToken =
  | { kind: "text"; value: string }
  | { kind: "citation"; slug: string };

const CITATION_RE = /\[wiki:([a-z][a-z0-9-]{0,95})\]/g;

export function splitCitationTokens(input: string): CitationToken[] {
  if (!input) return [];
  const tokens: CitationToken[] = [];
  let lastIndex = 0;
  // Reset the regex state every call — shared RegExp objects carry lastIndex
  // across invocations, which would silently truncate alternating matches.
  CITATION_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = CITATION_RE.exec(input)) !== null) {
    if (match.index > lastIndex) {
      tokens.push({ kind: "text", value: input.slice(lastIndex, match.index) });
    }
    tokens.push({ kind: "citation", slug: match[1] });
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < input.length) {
    tokens.push({ kind: "text", value: input.slice(lastIndex) });
  }
  return tokens;
}

export function isCitationToken(token: CitationToken): token is { kind: "citation"; slug: string } {
  return token.kind === "citation";
}