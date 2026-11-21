# Crowl web

React, TypeScript, and Vite frontend for Crowl. It provides the Schengen explorer, city price-history pages, and the read-only extractor status view.

## Local development

Requires Node.js 20 or newer.

```bash
npm ci
npm run dev
```

The dev server proxies `/api/*` to `VITE_DEV_API_TARGET` (default `http://localhost:8000`). Set `VITE_API_BASE_URL` when the browser should call another API base. The frontend does not substitute catalog or fare data when the API is unavailable; successful empty responses also remain empty.

The interactive map uses the keyless OpenFreeMap Positron style by default. Set `VITE_MAP_STYLE_URL` to use another MapLibre style and set `MAP_TILE_ORIGIN` to that style's tile origin so Caddy's content-security policy allows it.

## Verification

```bash
npm test
npm run build
npm audit
```

The production image builds the static bundle with Node 22, then serves it through Caddy. An unavailable API renders an error. Caddy proxies `/api/*` to the runtime `API_URL` environment variable and uses Railway's `PORT` when present.
