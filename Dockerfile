# syntax=docker/dockerfile:1
# Serviço do agente do Canteiro. O domínio vem do canteiro-api pelo commit travado no uv.lock (git só no build).
# Imagens por digest (índice multi-arch): a tag anda sozinha e o build deixaria de ser reprodutível. Quem atualiza o
# digest é o Dependabot (.github/dependabot.yml), num PR que passa pelo CI.
# Estágio só para o binário do uv: o Dependabot lê FROM, não o --mount=from= dos RUN.
FROM ghcr.io/astral-sh/uv:0.13@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
ENV UV_NO_CACHE=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /srv
# Versões do uv.lock (--frozen): o Render instala exatamente o que o CI testou, sem resolver de novo a cada build.
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src src
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
COPY --from=build /opt/venv /opt/venv
RUN useradd --create-home --uid 10001 canteiro
USER canteiro
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8100\")}/saude', timeout=4)"
# Sem --proxy-headers: o agente não usa o IP de quem chama, e o X-Forwarded-For é escrito pelo próprio cliente.
CMD ["sh", "-c", "uvicorn canteiro_agente.main:app --host 0.0.0.0 --port ${PORT:-8100}"]
