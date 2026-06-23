ARG ESBUILD_VERSION=0.21.5

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

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app

RUN pip install --prefix=/install .


FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY --from=builder /install /usr/local
COPY --from=esbuild /usr/local/bin/esbuild /usr/local/bin/esbuild
COPY app ./app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
