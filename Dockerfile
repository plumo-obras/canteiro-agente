# Serviço do agente do Canteiro. O domínio vem do canteiro-api pela tag do pyproject (precisa de git só no build).
FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
ENV UV_NO_CACHE=1
WORKDIR /srv
COPY pyproject.toml ./
COPY src src
RUN uv venv /opt/venv && uv pip install -p /opt/venv .

FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
COPY --from=build /opt/venv /opt/venv
RUN useradd --create-home --uid 10001 canteiro
USER canteiro
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8100\")}/saude', timeout=4)"
# --proxy-headers: atrás do balanceador do Render, o IP real vem no X-Forwarded-For dele (só ele alcança o contêiner).
CMD ["sh", "-c", "uvicorn canteiro_agente.main:app --host 0.0.0.0 --port ${PORT:-8100} --proxy-headers --forwarded-allow-ips='*'"]
