# Dentiva backend — Python 3.12 slim. Single image used for local dev (Docker
# Compose) and later AWS deployment.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for psycopg2-binary / cryptography wheels are already bundled,
# but build-essential helps if a wheel is missing for arm64.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY start.sh ./
RUN chmod +x start.sh

EXPOSE 8000

# Single source of truth (see start.sh): migrate, then bind $PORT (default 8000).
# Railway injects $PORT and health-checks that exact port — a hardcoded 8000
# would make the health-check hang and the deploy fail.
CMD ["bash", "start.sh"]
