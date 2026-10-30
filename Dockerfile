# Contexto de build = raiz do repositório (o domínio é um pacote local compartilhado).
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_NO_CACHE=1
WORKDIR /srv
COPY packages/dominio packages/dominio
COPY apps/agente apps/agente
RUN uv pip install --system ./packages/dominio ./apps/agente \
 && useradd --create-home --uid 10001 canteiro
USER canteiro
EXPOSE 8100
# --proxy-headers: atrás do balanceador do Render, o IP real vem no X-Forwarded-For dele (só ele alcança o contêiner).
CMD ["sh", "-c", "uvicorn canteiro_agente.main:app --host 0.0.0.0 --port ${PORT:-8100} --proxy-headers --forwarded-allow-ips='*'"]
