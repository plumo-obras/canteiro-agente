"""Serviço HTTP do agente: `POST /conversa` responde em SSE (eventos texto, ferramenta, proposta, fim, erro)."""
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from anthropic import AsyncAnthropic
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .chaves import FonteChaveApi, SemChaveCliente
from .config import Config
from .conversa import Dependencias, Evento, PedidoConversa, conversar
from .precos import PRECOS
from .sessoes import SessoesApi

log = logging.getLogger("canteiro_agente")


class CorpoConversa(BaseModel):
    texto: str = Field(min_length=1, max_length=8000)
    sessao_id: str | None = None
    obra_id: str | None = None


def formatar_sse(evento: Evento) -> str:
    nome, dados = evento
    return f"event: {nome}\ndata: {json.dumps(dados, ensure_ascii=False)}\n\n"


async def _sse(eventos: AsyncIterator[Evento]) -> AsyncIterator[str]:
    try:
        async for evento in eventos:
            yield formatar_sse(evento)
    except Exception:
        # Erro inesperado no meio do stream: o status 200 já foi enviado, então avisa pela própria SSE.
        log.exception("Falha na conversa")
        yield formatar_sse(("erro", {"mensagem": "Algo deu errado do nosso lado. Tente de novo."}))


def dependencias_padrao() -> Dependencias:
    config = Config.do_ambiente()
    if config.modelo not in PRECOS:
        # Sem preço não dá para medir o consumo: não sobe.
        raise RuntimeError(f"Modelo {config.modelo} sem preço em precos.PRECOS")
    http_api = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
    return Dependencias(
        config=config,
        http_api=http_api,
        sessoes=SessoesApi(http_api, config.api_url),
        fonte_chave=FonteChaveApi(http_api, config.api_url, config.token_servico) if config.token_servico
        else SemChaveCliente(),
        cliente_plataforma=AsyncAnthropic(api_key=config.anthropic_api_key) if config.anthropic_api_key else None,
        fabrica_cliente_proprio=lambda chave: AsyncAnthropic(api_key=chave),
    )


def criar_app(dep: Dependencias | None = None) -> FastAPI:
    @asynccontextmanager
    async def ciclo(app: FastAPI):
        app.state.dep = dep or dependencias_padrao()
        yield
        if dep is None:
            await app.state.dep.http_api.aclose()
            if app.state.dep.cliente_plataforma is not None:
                await app.state.dep.cliente_plataforma.close()

    app = FastAPI(title="Canteiro · agente", lifespan=ciclo)
    if dep is not None:
        app.state.dep = dep

    @app.post("/conversa")
    async def conversa(corpo: CorpoConversa, authorization: str | None = Header(default=None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Faça login para conversar com o assistente.")
        pedido = PedidoConversa(texto=corpo.texto, sessao_id=corpo.sessao_id, obra_id=corpo.obra_id)
        return StreamingResponse(
            _sse(conversar(app.state.dep, authorization, pedido)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/saude")
    async def saude():
        return {"ok": True}

    return app


app = criar_app()
