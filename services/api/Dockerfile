FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/packages/crowl-core:/app/services/api

WORKDIR /app

RUN addgroup --system crowl && adduser --system --ingroup crowl crowl

COPY services/api/requirements.txt /tmp/api-requirements.txt
RUN pip install --no-cache-dir -r /tmp/api-requirements.txt

COPY packages/crowl-core /app/packages/crowl-core
COPY services/api /app/services/api
COPY data /app/data
COPY migrations /app/migrations

WORKDIR /app

USER crowl

EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
