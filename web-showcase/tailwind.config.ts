import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        base: "#0F172A", // slate-900 page background
        surface: "#1E293B", // slate-800 cards
        raised: "#243247", // hover / nested surfaces
        line: "#334155", // slate-700 borders
        ink: "#E2E8F0", // primary text (slate-200)
        muted: "#94A3B8", // secondary text (slate-400)
        sky: { DEFAULT: "#38BDF8", deep: "#0EA5E9" },
        emerald: { DEFAULT: "#10B981", soft: "#34D399" },
        amber: { DEFAULT: "#F59E0B" },
        rose: { DEFAULT: "#F43F5E" },
      },
      fontFamily: {
        sans: ["var(--font-inter)", "ui-sans-serif", "system-ui", "Segoe UI", "sans-serif"],
        mono: ["var(--font-mono)", "JetBrains Mono", "Consolas", "ui-monospace", "monospace"],
      },
      keyframes: {
        pulseDot: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.35" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-400px 0" },
          "100%": { backgroundPosition: "400px 0" },
        },
      },
      animation: {
        "pulse-dot": "pulseDot 1.4s ease-in-out infinite",
        shimmer: "shimmer 1.4s linear infinite",
      },
    },
  },
  plugins: [],
};

export default config;
