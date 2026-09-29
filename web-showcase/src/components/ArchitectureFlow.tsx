// Static data-flow diagram (inline SVG, no client JavaScript).
const NODES = [
  { id: "win", x: 20, y: 40, title: "Windows HUD", lines: ["PyQt6 overlay", "Alt+C / Alt+V", "mss + sounddevice"] },
  { id: "gist", x: 290, y: 0, title: "GitHub Gist", lines: ["live tunnel URL", "60 s heartbeat"] },
  { id: "cf", x: 290, y: 120, title: "Cloudflare", lines: ["quick tunnel", "*.trycloudflare.com"] },
  { id: "kaggle", x: 560, y: 60, title: "Kaggle T4 GPU", lines: ["FastAPI node", "Qwen2-VL-7B NF4", "Whisper-base"] },
  { id: "local", x: 560, y: 200, title: "Local GPU", lines: ["same node on", "127.0.0.1:8000", "Qwen2-VL-2B NF4"] },
  { id: "web", x: 20, y: 200, title: "Vercel web app", lines: ["playground", "/api/fallback-infer", "Gemini / presets"] },
] as const;

const EDGES = [
  { from: [200, 70], to: [290, 30], label: "discover" },
  { from: [200, 95], to: [290, 150], label: "screenshot" },
  { from: [470, 150], to: [560, 100], label: "HTTPS" },
  { from: [200, 115], to: [560, 230], label: "local mode" },
  { from: [200, 240], to: [290, 165], label: "tier 1" },
  { from: [560, 70], to: [470, 30], label: "publish URL" },
] as const;

export function ArchitectureFlow() {
  return (
    <figure className="card overflow-x-auto p-4 sm:p-6">
      <svg
        viewBox="0 0 760 300"
        role="img"
        aria-labelledby="arch-title arch-desc"
        className="h-auto w-full min-w-[640px]"
        width={760}
        height={300}
      >
        <title id="arch-title">OmniSight data flow</title>
        <desc id="arch-desc">
          The Windows HUD discovers the Kaggle GPU node through a public gist and sends screenshots through a Cloudflare tunnel, or
          to a local GPU node. The web app tries the same node first and falls back to Gemini or verified presets.
        </desc>
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="#38BDF8" />
          </marker>
        </defs>
        {EDGES.map((edge) => (
          <g key={edge.label}>
            <line
              x1={edge.from[0]}
              y1={edge.from[1]}
              x2={edge.to[0]}
              y2={edge.to[1]}
              stroke="#38BDF8"
              strokeOpacity="0.55"
              strokeWidth="1.5"
              markerEnd="url(#arrow)"
            />
            <text
              x={(edge.from[0] + edge.to[0]) / 2}
              y={(edge.from[1] + edge.to[1]) / 2 - 6}
              fill="#94A3B8"
              fontSize="11"
              textAnchor="middle"
              fontFamily="var(--font-mono), monospace"
            >
              {edge.label}
            </text>
          </g>
        ))}
        {NODES.map((node) => (
          <g key={node.id} transform={`translate(${node.x} ${node.y})`}>
            <rect width="180" height={36 + node.lines.length * 16} rx="12" fill="#1E293B" stroke="#334155" />
            <text x="14" y="24" fill="#E2E8F0" fontSize="14" fontWeight="600">
              {node.title}
            </text>
            {node.lines.map((line, index) => (
              <text key={line} x="14" y={44 + index * 16} fill="#94A3B8" fontSize="11.5" fontFamily="var(--font-mono), monospace">
                {line}
              </text>
            ))}
          </g>
        ))}
      </svg>
      <figcaption className="mt-3 text-sm text-muted">
        One contract (Pydantic models exported as JSON Schema) is shared by the GPU node, the Windows client and this site.
      </figcaption>
    </figure>
  );
}
