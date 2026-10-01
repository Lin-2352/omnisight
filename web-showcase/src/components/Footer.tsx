import Link from "next/link";

import { GUIDE_URL, REPO_URL } from "./Header";

export function Footer() {
  return (
    <footer className="border-t border-line/60">
      <div className="mx-auto flex max-w-6xl flex-col gap-3 px-4 py-8 text-sm text-muted sm:flex-row sm:items-center sm:justify-between sm:px-6">
        <p>OmniSight &middot; MIT licensed</p>
        <nav aria-label="Footer" className="flex flex-wrap gap-x-5 gap-y-2">
          <Link href="/docs" className="hover:text-ink">
            Docs
          </Link>
          <a href={GUIDE_URL} rel="noopener noreferrer" target="_blank" className="hover:text-ink">
            User guide
          </a>
          <a href={REPO_URL} rel="noopener noreferrer" target="_blank" className="hover:text-ink">
            Source
          </a>
          <Link href="/docs#api" className="hover:text-ink">
            API
          </Link>
        </nav>
      </div>
    </footer>
  );
}
