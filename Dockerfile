# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=sopds.settings \
    DB_NAME=sopds \
    DB_USER=sopds \
    DB_PASS=sopds \
    DB_HOST=db \
    DB_PORT=5432 \
    TIME_ZONE=Europe/Moscow \
    DEBUG=False

WORKDIR /sopds

# System dependencies (runtime only; wheels are used for Python packages)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Directory for the book collection (mounted as a volume)
RUN mkdir -p /sopds/books /sopds/staticfiles /sopds/opds_catalog/tmp /sopds/opds_catalog/log \
    && chmod +x docker-entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["gunicorn", "sopds.wsgi:application", "--bind", "0.0.0.0:8080", "--workers", "3", "--timeout", "120"]
