# Crowl web

React, TypeScript, and Vite frontend for Crowl. It provides the Schengen explorer, city price-history pages, and the read-only extractor status view.

## Local development

Requires Node.js 20 or newer.

```bash
npm ci
npm run dev
```

The dev server proxies `/api/*` to `VITE_DEV_API_TARGET` (default `http://localhost:8000`). Set `VITE_API_BASE_URL` when the browser should call another API base. When `VITE_ENABLE_MOCK_FALLBACK=true`, an unavailable API activates clearly labeled preview data. Successful empty API responses remain empty and are never replaced with mock fares.

The backend's seeded fixture fares are independently detected through a zero requested-source scope (or the hidden `fixture` booking source) and labeled as synthetic, even though the API request itself succeeds.

## Verification

```bash
npm test
npm run build
npm audit
```

The production image builds the static bundle with Node 22, then serves it through Caddy. Production mock fallback defaults to `false`: an unavailable API renders an error instead of preview fares. Caddy proxies `/api/*` to the runtime `API_URL` environment variable and uses Railway's `PORT` when present.
