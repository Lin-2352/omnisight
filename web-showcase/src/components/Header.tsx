import { GitBranch, ScanEye } from "lucide-react";
import Link from "next/link";

import { StatusBadge } from "./StatusBadge";

export const REPO_URL = process.env.NEXT_PUBLIC_REPO_URL || "https://github.com/Lin-2352/omnisight";

export const GUIDE_URL = `${REPO_URL}/blob/main/docs/USER_GUIDE.md`;

export function Header() {
  return (
    <header className="sticky top-0 z-40 border-b border-line/70 bg-base/85 backdrop-blur">
      <div className="mx-auto flex h-16 max-w-6xl items-center gap-6 px-4 sm:px-6">
        <Link href="/" className="flex items-center gap-2 font-semibold text-ink">
          <ScanEye aria-hidden className="h-5 w-5 text-sky" />
          OmniSight
        </Link>
        <nav aria-label="Main" className="hidden items-center gap-5 text-sm text-muted md:flex">
          <Link href="/#features-title" className="hover:text-ink">
            Features
          </Link>
          <Link href="/#playground" className="hover:text-ink">
            Playground
          </Link>
          <Link href="/#architecture" className="hover:text-ink">
            Architecture
          </Link>
          <Link href="/docs" className="hover:text-ink">
            Docs
          </Link>
          <a href={GUIDE_URL} rel="noopener noreferrer" target="_blank" className="hover:text-ink">
            Guide
          </a>
        </nav>
        <div className="ml-auto flex items-center gap-3">
          <StatusBadge className="hidden lg:inline-flex" />
          <a
            href={REPO_URL}
            className="inline-flex h-8 items-center gap-2 rounded-full border border-line px-3 text-xs font-medium text-ink hover:bg-surface"
            rel="noopener noreferrer"
            target="_blank"
          >
            <GitBranch aria-hidden className="h-4 w-4" />
            GitHub
          </a>
        </div>
      </div>
    </header>
  );
}
