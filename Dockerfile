ARG ESBUILD_VERSION=0.21.5
ARG UV_VERSION=0.12.13

# Standalone esbuild CLI for the LLM syntax validator: the app calls it via
# subprocess, so node/npm are build-time only and stay out of the runtime image.
FROM node:26-slim AS esbuild

ARG ESBUILD_VERSION

WORKDIR /esbuild

RUN npm install --no-save --no-audit --no-fund "esbuild@${ESBUILD_VERSION}" \
    && ESBUILD_BINARY="$(find node_modules/@esbuild -path '*/bin/esbuild' -type f | head -n 1)" \
    && test -n "${ESBUILD_BINARY}" \
    && cp "${ESBUILD_BINARY}" /usr/local/bin/esbuild \
    && chmod +x /usr/local/bin/esbuild \
    && /usr/local/bin/esbuild --version


FROM python:3.14-slim AS builder

ARG UV_VERSION

COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# Install dependencies into /opt/venv using lockfile
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Copy application code and install project non-editable
COPY app ./app
COPY scripts ./scripts
RUN uv sync --locked --no-dev --no-editable


FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY --from=builder /opt/venv /opt/venv
COPY --from=esbuild /usr/local/bin/esbuild /usr/local/bin/esbuild
COPY app ./app
COPY scripts ./scripts

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

