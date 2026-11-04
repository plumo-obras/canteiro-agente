"""Serviço HTTP do agente: `POST /conversa` responde em SSE (eventos texto, descartar, ferramenta, proposta, fim, erro;
comentário `: ping` nos silêncios)."""
import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from anthropic import AsyncAnthropic
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .chaves import FonteChaveApi, SemChaveCliente
from .config import Config
from .conversa import Dependencias, Evento, PedidoConversa, aguardar_pendentes, conversar
from .precos import PRECOS
from .sessoes import SessoesApi

log = logging.getLogger("canteiro_agente")


class CorpoConversa(BaseModel):
    texto: str = Field(min_length=1, max_length=8000)
    # Os ids seguem o formato de `api_central._ID` (até 64): sem teto, um corpo enorme iria parar na API e no histórico.
    sessao_id: str | None = Field(default=None, max_length=64)
    obra_id: str | None = Field(default=None, max_length=64)


# Mensagem por campo do corpo: a tela mostra o `detail` como veio, então ele tem de ser frase, não a lista do Pydantic.
_CAMPOS = {
    "texto": "A pergunta tem de ter de 1 a 8.000 caracteres.",
    "sessao_id": "Identificador de conversa inválido.",
    "obra_id": "Identificador de obra inválido.",
}


def mensagem_de_validacao(e: RequestValidationError) -> str:
    campos = [str(err["loc"][1]) for err in e.errors() if len(err.get("loc", ())) > 1 and err["loc"][0] == "body"]
    for campo in campos:
        if campo in _CAMPOS:
            return _CAMPOS[campo]
    return "Pedido inválido: envie {texto, sessao_id?, obra_id?} em JSON."


def formatar_sse(evento: Evento) -> str:
    nome, dados = evento
    if nome.startswith(":"):
        return f"{nome}\n\n"   # comentário SSE (o ping): o leitor da web ignora linha que começa com ":"
    return f"event: {nome}\ndata: {json.dumps(dados, ensure_ascii=False)}\n\n"


async def _sse(eventos: AsyncIterator[Evento]) -> AsyncIterator[str]:
    try:
        async for evento in eventos:
            yield formatar_sse(evento)
    except Exception:
        # Erro inesperado no meio do stream: o status 200 já foi enviado, então avisa pela própria SSE.
        log.exception("Falha na conversa")
        yield formatar_sse(("erro", {"mensagem": "Algo deu errado do nosso lado. Tente de novo."}))


def _cliente_plataforma(config: Config) -> AsyncAnthropic | None:
    if config.modelo_roteiro:
        # Modo de demonstração: o SDK de verdade conversa com o modelo de roteiro no mesmo processo, sem rede.
        # Para usar o Claude, desligue MODELO_ROTEIRO e defina ANTHROPIC_API_KEY.
        import httpx2
        from anthropic import DefaultAsyncHttpxClient

        from .modelo_roteiro import criar_app as criar_roteiro
        return AsyncAnthropic(api_key="roteiro", base_url="http://roteiro", max_retries=0,
                              http_client=DefaultAsyncHttpxClient(transport=httpx2.ASGITransport(app=criar_roteiro())))
    return AsyncAnthropic(api_key=config.anthropic_api_key) if config.anthropic_api_key else None


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
        cliente_plataforma=_cliente_plataforma(config),
        fabrica_cliente_proprio=lambda chave: AsyncAnthropic(api_key=chave),
    )


def criar_app(dep: Dependencias | None = None) -> FastAPI:
    @asynccontextmanager
    async def ciclo(app: FastAPI):
        app.state.dep = dep or dependencias_padrao()
        yield
        # Consumo de conversa cancelada ainda pode estar a caminho da API: não desliga antes de gravar.
        try:
            await asyncio.wait_for(aguardar_pendentes(), 10)
        except TimeoutError:
            log.error("Desligando com gravações de consumo pendentes")
        if dep is None:
            await app.state.dep.http_api.aclose()
            if app.state.dep.cliente_plataforma is not None:
                await app.state.dep.cliente_plataforma.close()

    app = FastAPI(title="Canteiro · agente", lifespan=ciclo)
    if dep is not None:
        app.state.dep = dep

    @app.exception_handler(RequestValidationError)
    async def _invalido(_: Request, e: RequestValidationError):
        # o contrato é {detail: "mensagem pronta"} em todo erro; sem isto sairia a lista crua do Pydantic
        return JSONResponse(status_code=422, content={"detail": mensagem_de_validacao(e)})

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
