// Safe markdown handling (no HTML is ever produced or injected).
// A TypeScript port of shared/omnisight_contracts/markdown.py plus a small inline tokenizer
// that the UI renders into React elements.

export interface MarkdownSegment {
  kind: "prose" | "code";
  text: string;
  language: string;
}

const OPEN_FENCE = /^( {0,3})(`{3,}|~{3,})([^\n]*)$/;

const LANGUAGE_ALIASES: Record<string, string> = {
  py: "python", python3: "python", js: "javascript", jsx: "javascript", mjs: "javascript",
  ts: "typescript", tsx: "typescript", rs: "rust", sh: "bash", shell: "bash", zsh: "bash",
  console: "bash", ps1: "powershell", pwsh: "powershell", yml: "yaml", "c++": "cpp", cc: "cpp",
  hpp: "cpp", cs: "csharp", golang: "go", txt: "text", plaintext: "text",
};

export function normalizeLanguage(info: string): string {
  const token = (info.trim().split(/\s+/)[0] ?? "").replace(/^[{.]+|[}.]+$/g, "").toLowerCase();
  if (!token) return "text";
  return (LANGUAGE_ALIASES[token] ?? token).slice(0, 32);
}

export function splitMarkdownSegments(markdown: string): MarkdownSegment[] {
  const segments: MarkdownSegment[] = [];
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  let prose: string[] = [];
  const flush = () => {
    const text = prose.join("\n").replace(/^\n+|\n+$/g, "");
    if (text.trim()) segments.push({ kind: "prose", text, language: "text" });
    prose = [];
  };
  let index = 0;
  while (index < lines.length) {
    const line = lines[index] ?? "";
    const match = OPEN_FENCE.exec(line);
    if (!match || (match[2]?.startsWith("`") && (match[3] ?? "").includes("`"))) {
      prose.push(line);
      index += 1;
      continue;
    }
    flush();
    const fence = match[2] ?? "```";
    const indent = (match[1] ?? "").length;
    const close = new RegExp(`^ {0,3}${fence[0] === "`" ? "`" : "~"}{${fence.length},}[ \\t]*$`);
    const body: string[] = [];
    index += 1;
    while (index < lines.length && !close.test(lines[index] ?? "")) {
      const raw = lines[index] ?? "";
      const removable = raw.length - raw.trimStart().length;
      body.push(raw.slice(Math.min(indent, removable)));
      index += 1;
    }
    index += 1;
    segments.push({ kind: "code", text: body.join("\n"), language: normalizeLanguage(match[3] ?? "") });
  }
  flush();
  return segments;
}

export function extractCodeBlocks(markdown: string): { language: string; code: string }[] {
  return splitMarkdownSegments(markdown)
    .filter((segment) => segment.kind === "code")
    .map((segment) => ({ language: segment.language, code: segment.text }));
}

/** First prose paragraph as plain text, truncated on a word boundary (port of derive_summary). */
export function deriveSummary(markdown: string, maxChars = 500): string {
  const prose = splitMarkdownSegments(markdown)
    .filter((segment) => segment.kind === "prose")
    .map((segment) => segment.text)
    .join("\n\n");
  let chosen = "";
  for (const paragraph of prose.split(/\n\s*\n/)) {
    const lines = paragraph
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line && !/^[#|>]/.test(line) && !/^([-*_])\s*(\1\s*){2,}$/.test(line));
    if (lines.length) {
      chosen = lines.join(" ");
      break;
    }
  }
  chosen = chosen
    .replace(/^[-*+]\s+|^\d+[.)]\s+/, "")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/(^|[^\w])(\*\*|__|\*|_)(\S(?:.*?\S)?)\2(?!\w)/g, "$1$3")
    .replace(/\s+/g, " ")
    .trim();
  if (chosen.length <= maxChars) return chosen;
  let cut = chosen.slice(0, maxChars - 1);
  const boundary = cut.lastIndexOf(" ");
  if (boundary >= maxChars / 2) cut = cut.slice(0, boundary);
  return `${cut.replace(/[ ,;:.]+$/, "")}…`;
}

export type InlineToken =
  | { type: "text"; value: string }
  | { type: "code"; value: string }
  | { type: "strong"; value: string }
  | { type: "link"; value: string; href: string };

const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\((https?:\/\/[^)\s]+)\))/g;

/** Split a line into text / code / bold / http(s)-link tokens. Anything else stays literal text. */
export function tokenizeInline(line: string): InlineToken[] {
  const tokens: InlineToken[] = [];
  let last = 0;
  for (const match of line.matchAll(INLINE)) {
    const start = match.index ?? 0;
    if (start > last) tokens.push({ type: "text", value: line.slice(last, start) });
    if (match[1]) tokens.push({ type: "code", value: match[1].slice(1, -1) });
    else if (match[2]) tokens.push({ type: "strong", value: match[2].slice(2, -2) });
    else if (match[3] && match[4]) {
      tokens.push({ type: "link", value: match[3].slice(1, match[3].indexOf("]")), href: match[4] });
    }
    last = start + match[0].length;
  }
  if (last < line.length) tokens.push({ type: "text", value: line.slice(last) });
  return tokens;
}

export type ProseBlock =
  | { type: "heading"; text: string }
  | { type: "paragraph"; text: string }
  | { type: "list"; ordered: boolean; items: string[] };

/** Group prose into headings, paragraphs and lists (no HTML, no nesting). */
export function parseProse(text: string): ProseBlock[] {
  const blocks: ProseBlock[] = [];
  for (const chunk of text.split(/\n\s*\n/)) {
    const lines = chunk.split("\n").map((line) => line.trimEnd()).filter((line) => line.trim());
    if (!lines.length) continue;
    const bullet = lines.every((line) => /^\s*[-*+]\s+/.test(line));
    const numbered = lines.every((line) => /^\s*\d+[.)]\s+/.test(line));
    if (bullet || numbered) {
      blocks.push({
        type: "list",
        ordered: numbered,
        items: lines.map((line) => line.replace(/^\s*(?:[-*+]|\d+[.)])\s+/, "")),
      });
    } else if (lines.length === 1 && /^#{1,6}\s+/.test(lines[0] ?? "")) {
      blocks.push({ type: "heading", text: (lines[0] ?? "").replace(/^#{1,6}\s+/, "") });
    } else {
      blocks.push({ type: "paragraph", text: lines.join(" ") });
    }
  }
  return blocks;
}
