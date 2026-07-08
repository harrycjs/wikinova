import { describe, expect, it } from "vitest";

import { splitCitationTokens } from "@/lib/markdown/citation-parser";

describe("splitCitationTokens", () => {
  it("returns a single text token for input with no citations", () => {
    const tokens = splitCitationTokens("hello world");
    expect(tokens).toEqual([{ kind: "text", value: "hello world" }]);
  });

  it("extracts a single citation at the start", () => {
    const tokens = splitCitationTokens("[wiki:intro] the answer is 42");
    expect(tokens).toEqual([
      { kind: "citation", slug: "intro" },
      { kind: "text", value: " the answer is 42" },
    ]);
  });

  it("extracts a single citation in the middle", () => {
    const tokens = splitCitationTokens("Before [wiki:foo] after");
    expect(tokens).toEqual([
      { kind: "text", value: "Before " },
      { kind: "citation", slug: "foo" },
      { kind: "text", value: " after" },
    ]);
  });

  it("extracts multiple citations in the same string", () => {
    const tokens = splitCitationTokens(
      "see [wiki:alpha] and [wiki:beta-2] for details",
    );
    expect(tokens).toEqual([
      { kind: "text", value: "see " },
      { kind: "citation", slug: "alpha" },
      { kind: "text", value: " and " },
      { kind: "citation", slug: "beta-2" },
      { kind: "text", value: " for details" },
    ]);
  });

  it("leaves bracketed text alone when slug rules are violated", () => {
    // Slugs must start with a lowercase ASCII letter.
    const tokens = splitCitationTokens("[wiki:1abc] and [wiki:Capital]");
    expect(tokens).toEqual([
      { kind: "text", value: "[wiki:1abc] and [wiki:Capital]" },
    ]);
  });

  it("handles empty input", () => {
    expect(splitCitationTokens("")).toEqual([]);
  });

  it("resets shared regex state between calls", () => {
    // Calling twice in a row must yield the same tokens (lastIndex guard).
    const a = splitCitationTokens("[wiki:reset-test] x");
    const b = splitCitationTokens("[wiki:reset-test] x");
    expect(a).toEqual(b);
    expect(a).toEqual([
      { kind: "citation", slug: "reset-test" },
      { kind: "text", value: " x" },
    ]);
  });

  it("truncates slugs longer than the agent-side regex allows", () => {
    const longSlug = "a".repeat(120);
    const tokens = splitCitationTokens(`[wiki:${longSlug}] tail`);
    // The regex caps slug capture at 96 chars; anything longer is left as text.
    expect(tokens).toEqual([{ kind: "text", value: `[wiki:${longSlug}] tail` }]);
  });
});