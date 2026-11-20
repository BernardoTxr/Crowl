FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CROWL_DATA_DIR=/app/data

WORKDIR /app

RUN addgroup --system crowl && adduser --system --ingroup crowl crowl

COPY packages/crowl-core /build/crowl-core
COPY services/crawler /build/crawler
COPY data /app/data

RUN python -m pip install --no-cache-dir /build/crowl-core /build/crawler \
    && rm -rf /build

USER crowl

ENTRYPOINT ["crowl-crawler"]
CMD ["run"]
