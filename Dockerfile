# Production image. The paid workbooks are pre-built in products/dist (LibreOffice is NOT needed at runtime).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HUB_ENV=production \
    HUB_PRODUCT_DIR=/app/products/dist/full \
    HUB_SAMPLE_DIR=/app/products/dist/sample

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY hub ./hub
COPY scripts ./scripts
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY products/catalog.json ./products/catalog.json
COPY products/dist ./products/dist

RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown -R app /app/data
USER app

EXPOSE 8000
# Behind the host's HTTPS load balancer. Set HUB_FORWARDED_ALLOW_IPS to the proxy range your host documents.
CMD ["sh", "-c", "uvicorn hub.app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=${HUB_FORWARDED_ALLOW_IPS:-127.0.0.1} --workers 1"]
