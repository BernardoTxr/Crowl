FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CROWL_DATA_DIR=/app/data \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN addgroup --system crowl && adduser --system --ingroup crowl crowl

COPY packages/crowl-core /build/crowl-core
COPY services/crawler /build/crawler

RUN apt-get update \
    && apt-get install --no-install-recommends -y libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir /build/crowl-core /build/crawler \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /build /var/lib/apt/lists/*

# Catalog-only edits should not invalidate the large browser/dependency layer.
COPY data /app/data

USER crowl

ENTRYPOINT ["crowl-crawler"]
CMD ["run"]
