import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import type { ReactNode } from "react";

import "./globals.css";

// display "optional": if a font misses the first ~100 ms the metric-matched fallback stays for
// that page view instead of swapping in later, which keeps CLS at 0 and LCP off the font's path.
const inter = Inter({ subsets: ["latin"], display: "optional", variable: "--font-inter" });
const mono = JetBrains_Mono({ subsets: ["latin"], display: "optional", variable: "--font-mono", weight: ["400", "600"] });

export const metadata: Metadata = {
  metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_URL || "https://omnisight-nine.vercel.app"),
  title: {
    default: "OmniSight - multimodal screen intelligence for developers",
    template: "%s · OmniSight",
  },
  description:
    "Press a hotkey, and a vision-language model on a GPU reads your screen and explains the error. Windows HUD, Kaggle or local GPU brain, and a zero-downtime web playground.",
  openGraph: {
    title: "OmniSight",
    description: "Screenshot in, diagnosis out: Qwen2-VL on a Kaggle T4 or your own GPU, behind a Windows HUD.",
    type: "website",
  },
  robots: { index: true, follow: true },
  // The site is dark by design; ask dark-mode extensions (Dark Reader) not to recolor it.
  other: { "darkreader-lock": "true" },
};

export const viewport: Viewport = {
  themeColor: "#0F172A",
  colorScheme: "dark",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${mono.variable}`}>
      <body className="min-h-screen">{children}</body>
    </html>
  );
}
