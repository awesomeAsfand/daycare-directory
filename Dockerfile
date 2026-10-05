# ── Stage 1: base ────────────────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps (Playwright needs these too)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*
# postgresql-client provides pg_isready, which entrypoint.sh waits on

# ── Stage 2: dependencies ─────────────────────────────────────
FROM base AS deps

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ── Stage 3: dev ──────────────────────────────────────────────
FROM deps AS dev

# Install Playwright browsers (for the scraper)
RUN playwright install --with-deps chromium

COPY . .
EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# ── Stage 4: production ───────────────────────────────────────
FROM deps AS prod

COPY . .

# Executable whatever the checkout's file mode (e.g. cloned on Windows)
RUN chmod +x /app/entrypoint.sh

# Collect with production storage (hashed, compressed files); the real
# SECRET_KEY isn't needed for this step
RUN DEBUG=False SECRET_KEY=collectstatic-only python manage.py collectstatic --noinput

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
