# web-showcase

The Next.js (TypeScript) showcase and playground, deployable to Vercel or Netlify. Built in Phase 4.

- A hero section with the architecture overview, the Windows installer download, and a live backend status indicator.
- `DemoPlayground`: drag-and-drop screenshot analysis. It uses the live Kaggle tunnel when it is online and falls back to `/api/fallback-infer` (Gemini or a deterministic evaluator) when it is not.
- `/docs`: sequence diagrams, benchmarks, and setup guides.

Request and response types come from `../shared/schema/*.schema.json`.
