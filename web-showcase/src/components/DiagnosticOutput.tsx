"use client";

import { Check, Cloud, Copy, Terminal, Zap } from "lucide-react";
import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";

import { cn } from "@/lib/cn";
import { parseProse, splitMarkdownSegments, tokenizeInline, type MarkdownSegment } from "@/lib/markdown";
import type { AnalysisResult } from "@/lib/types";

const SHELL = new Set(["bash", "sh", "powershell", "cmd", "console", "shell", "zsh", "bat"]);

// --- minimal syntax highlighter (React elements only; never HTML strings) ----------------

const KEYWORDS: Record<string, string[]> = {
  python: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None not or pass raise return True try while with yield".split(" "),
  typescript: "abstract as async await break case catch class const continue default else enum export extends false finally for from function if implements import in interface let new null return static super switch this throw true try type typeof undefined var void while".split(" "),
  javascript: "async await break case catch class const continue default else export extends false finally for from function if import in let new null return super switch this throw true try typeof undefined var void while".split(" "),
  tsx: [],
  cpp: "auto bool break case char class const continue default delete do double else enum explicit extern false float for if include int long namespace new nullptr private protected public return short signed sizeof static struct switch template this throw true try typedef typename unsigned using virtual void while".split(" "),
  rust: "as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while".split(" "),
  bash: "if then else elif fi for while do done case esac function in export local return sudo cd echo".split(" "),
};
KEYWORDS.tsx = KEYWORDS.typescript ?? [];

const TOKEN_CLASSES = {
  comment: "text-muted italic",
  string: "text-emerald-soft",
  keyword: "text-sky",
  number: "text-amber",
  call: "text-[#C4B5FD]",
} as const;

function highlightLine(line: string, language: string): ReactNode[] {
  const keywords = KEYWORDS[language] ?? [];
  const commentPattern = language === "python" || language === "bash" ? "#.*$" : "//.*$";
  const parts = [
    `(?<comment>${commentPattern})`,
    `(?<string>"(?:[^"\\\\]|\\\\.)*"|'(?:[^'\\\\]|\\\\.)*'|\`[^\`]*\`)`,
    keywords.length ? `(?<keyword>\\b(?:${keywords.map((word) => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})\\b)` : null,
    "(?<number>\\b\\d+(?:\\.\\d+)?\\b)",
    "(?<call>\\b[A-Za-z_]\\w*(?=\\s*\\())",
  ].filter(Boolean);
  const pattern = new RegExp(parts.join("|"), "g");
  const nodes: ReactNode[] = [];
  let last = 0;
  for (const match of line.matchAll(pattern)) {
    const start = match.index ?? 0;
    if (start > last) nodes.push(line.slice(last, start));
    const kind = (Object.keys(match.groups ?? {}) as (keyof typeof TOKEN_CLASSES)[]).find((key) => match.groups?.[key] !== undefined);
    nodes.push(
      <span key={`${start}-${kind}`} className={kind ? TOKEN_CLASSES[kind] : undefined}>
        {match[0]}
      </span>,
    );
    last = start + match[0].length;
  }
  if (last < line.length) nodes.push(line.slice(last));
  return nodes;
}

// --- copy button ---------------------------------------------------------------------------

function CopyButton({ text, label, className }: { text: string; label: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1400);
    } catch {
      setCopied(false);
    }
  };
  return (
    <button
      type="button"
      onClick={copy}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-semibold transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-sky",
        copied ? "border-emerald/60 bg-emerald/15 text-emerald" : "border-line bg-base text-ink hover:bg-raised",
        className,
      )}
    >
      {copied ? <Check aria-hidden className="h-3.5 w-3.5" /> : <Copy aria-hidden className="h-3.5 w-3.5" />}
      {copied ? "Copied!" : label}
    </button>
  );
}

// --- rendering ------------------------------------------------------------------------------

function Inline({ text }: { text: string }) {
  return (
    <>
      {tokenizeInline(text).map((token, index) => {
        if (token.type === "code") {
          return (
            <code key={index} className="rounded bg-base px-1.5 py-0.5 font-mono text-[0.85em] text-sky">
              {token.value}
            </code>
          );
        }
        if (token.type === "strong") return <strong key={index} className="text-ink">{token.value}</strong>;
        if (token.type === "link") {
          return (
            <a key={index} href={token.href} rel="noopener noreferrer nofollow" target="_blank" className="text-sky underline underline-offset-2">
              {token.value}
            </a>
          );
        }
        return <Fragment key={index}>{token.value}</Fragment>;
      })}
    </>
  );
}

function CodeBlock({ segment }: { segment: MarkdownSegment }) {
  return (
    <div className="overflow-hidden rounded-xl border border-line bg-[#0B1220]">
      <div className="flex items-center justify-between border-b border-line/70 px-3 py-1.5">
        <span className="font-mono text-[11px] uppercase tracking-wider text-muted">{segment.language}</span>
        <CopyButton text={segment.text} label="Copy" />
      </div>
      <pre className="overflow-x-auto p-3 font-mono text-[13px] leading-relaxed text-ink">
        <code>
          {segment.text.split("\n").map((line, index) => (
            <span key={index} className="block min-h-[1.25em]">
              {highlightLine(line, segment.language)}
            </span>
          ))}
        </code>
      </pre>
    </div>
  );
}

export function DiagnosticOutput({ result }: { result: AnalysisResult }) {
  const { response, tier, banner, latency } = result;
  const segments = splitMarkdownSegments(response.markdown);
  const firstProse = segments[0]?.kind === "prose" ? segments[0].text.split(/\n\s*\n/)[0] ?? "" : "";
  const plain = (text: string) => text.replace(/[`*_]/g, "").replace(/\s+/g, " ").trim().replace(/\.$/, "").toLowerCase();
  // Don't repeat the summary: drop the first paragraph when it says the same thing.
  const body =
    firstProse && plain(firstProse) === plain(response.summary)
      ? [{ ...(segments[0] as MarkdownSegment), text: (segments[0] as MarkdownSegment).text.split(/\n\s*\n/).slice(1).join("\n\n") }, ...segments.slice(1)]
      : segments;
  const code = segments.filter((segment) => segment.kind === "code");
  const fix = code.find((segment) => !SHELL.has(segment.language));
  const command = code.find((segment) => SHELL.has(segment.language));

  return (
    <div className="mt-4 flex flex-col gap-4">
      {banner && (
        <p className="flex items-center gap-2 rounded-lg border border-amber/40 bg-amber/10 px-3 py-2 font-mono text-xs text-amber">
          <Cloud aria-hidden className="h-4 w-4 shrink-0" />
          {banner}
        </p>
      )}
      <p className="text-lg font-semibold leading-snug text-ink">{response.summary}</p>
      {body.map((segment, index) =>
        segment.kind === "code" ? (
          <CodeBlock key={index} segment={segment} />
        ) : (
          <div key={index} className="space-y-3 text-sm leading-relaxed text-muted">
            {parseProse(segment.text).map((block, blockIndex) =>
              block.type === "heading" ? (
                <h4 key={blockIndex} className="font-semibold text-ink">
                  <Inline text={block.text} />
                </h4>
              ) : block.type === "list" ? (
                <ul key={blockIndex} className={cn("space-y-1 pl-5", block.ordered ? "list-decimal" : "list-disc")}>
                  {block.items.map((item, itemIndex) => (
                    <li key={itemIndex}>
                      <Inline text={item} />
                    </li>
                  ))}
                </ul>
              ) : (
                <p key={blockIndex}>
                  <Inline text={block.text} />
                </p>
              ),
            )}
          </div>
        ),
      )}
      {(fix || command) && (
        <div className="flex flex-wrap gap-2">
          {fix && <CopyButton text={fix.text} label="Copy Fix" className="px-3 py-1.5 text-sm" />}
          {command && (
            <span className="inline-flex items-center gap-2">
              <Terminal aria-hidden className="h-4 w-4 text-muted" />
              <CopyButton text={command.text} label="Copy Terminal Command" className="px-3 py-1.5 text-sm" />
            </span>
          )}
        </div>
      )}
      <dl className="mt-1 grid grid-cols-2 gap-x-4 gap-y-1 border-t border-line pt-3 font-mono text-[11px] text-muted sm:grid-cols-4">
        <div>
          <dt className="uppercase tracking-wider">engine</dt>
          <dd className="text-ink">{tier}</dd>
        </div>
        <div>
          <dt className="uppercase tracking-wider">round trip</dt>
          <dd className="text-ink">{(latency.networkMs / 1000).toFixed(1)} s</dd>
        </div>
        <div>
          {/* Only the GPU node streams, so only it has a real time-to-first-token. */}
          <dt className="uppercase tracking-wider">{tier === "kaggle" ? "first token" : "engine time"}</dt>
          <dd className="text-ink">
            {tier === "kaggle"
              ? `${(latency.serverTtftMs / 1000).toFixed(1)} s`
              : tier === "gemini"
                ? `${(latency.serverTotalMs / 1000).toFixed(1)} s`
                : "instant"}
          </dd>
        </div>
        <div>
          <dt className="flex items-center gap-1 uppercase tracking-wider">
            <Zap aria-hidden className="h-3 w-3" />
            tokens/s
          </dt>
          <dd className="text-ink">{latency.tokensPerSec ? latency.tokensPerSec.toFixed(1) : "n/a"}</dd>
        </div>
      </dl>
      <p className="font-mono text-[11px] text-muted/80">
        model {response.model_id}
        {result.trace ? ` · route ${result.trace}` : ""}
        {latency.compressMs ? ` · compress ${latency.compressMs} ms` : ""}
      </p>
    </div>
  );
}
