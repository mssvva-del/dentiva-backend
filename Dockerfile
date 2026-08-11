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
# scripts/ holds the pre-deploy migration runner that railway.toml invokes. This
# COPY list is an allow-list: a file added to the repo is NOT in the image until
# it is named here, and the omission shows up as "No such file or directory" at
# deploy time rather than anywhere earlier. tests/test_container_has_what_it_runs.py
# checks that every path the deploy config runs is present.
COPY scripts ./scripts
RUN chmod +x start.sh scripts/*.sh

EXPOSE 8000

# Single source of truth (see start.sh): migrate, then bind $PORT (default 8000).
# Railway injects $PORT and health-checks that exact port — a hardcoded 8000
# would make the health-check hang and the deploy fail.
CMD ["bash", "start.sh"]
