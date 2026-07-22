# syntax=docker/dockerfile:1

# -----------------------------
# Stage 1: builder (install deps)
# -----------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip==26.0.1 wheel==0.46.3 setuptools

COPY app/requirements.txt /app/requirements.txt
RUN python -m pip install --prefix=/install -r /app/requirements.txt


# -----------------------------
# Stage 2: runtime
# -----------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TMPDIR=/tmp \
    # Ensure gunicorn uses a writable temp dir (in addition to CLI flag below)
    GUNICORN_CMD_ARGS="--worker-tmp-dir /tmp" \
    # Avoid python trying to write __pycache__ into read-only directories
    PYTHONPYCACHEPREFIX=/tmp/pycache

WORKDIR /app

# gosu lets us drop privileges after fixing /tmp perms
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/*

# Copy installed deps from builder into runtime
COPY --from=builder /install /usr/local

# Copy application
COPY app /app

# Create non-root user (we will drop to this user in entrypoint)
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

# Entrypoint: fix temp dirs permissions (important when /tmp is volume-mounted),
# then run the command as appuser.
RUN cat > /usr/local/bin/entrypoint.sh <<'EOF' \
  && chmod +x /usr/local/bin/entrypoint.sh
#!/bin/sh
set -eu

# Ensure temp dirs exist. In ECS with volume-mounted /tmp, permissions can be restrictive.
mkdir -p /tmp /var/tmp /usr/tmp /tmp/pycache

# Sticky-bit so any user can create temp files safely
chmod 1777 /tmp /var/tmp /usr/tmp || true
chmod 777 /tmp/pycache || true

# Drop privileges and run
exec gosu appuser "$@"
EOF

EXPOSE 5000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Also pass --worker-tmp-dir explicitly (belt + suspenders)
CMD ["gunicorn", "-b", "0.0.0.0:5000", "--worker-tmp-dir", "/tmp", "app:app"]
