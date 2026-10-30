"""De onde vem a chave da Anthropic de cada conversa.

- Cliente de créditos: chave da plataforma (`ANTHROPIC_API_KEY`, só em variável de ambiente).
- Cliente com chave própria (`GET /creditos` → `chave_propria: true`): a chave dele, cifrada na API, entregue por
  `GET /interno/chave-anthropic` (token de serviço + token do usuário). A chave nunca vai para log nem para disco, e
  sem ela a conversa termina com erro: cair para a chave da plataforma faria a plataforma pagar o consumo do cliente.
"""
import logging
from typing import Protocol

import httpx

from .api_central import CABECALHO_USUARIO

log = logging.getLogger("canteiro_agente")


class FonteChaveCliente(Protocol):
    async def chave_do_cliente(self, autorizacao_usuario: str) -> str | None: ...


class SemChaveCliente:
    async def chave_do_cliente(self, autorizacao_usuario: str) -> str | None:
        return None


class FonteChaveApi:
    def __init__(self, http: httpx.AsyncClient, base_url: str, token_servico: str):
        self._http = http
        self._url = base_url.rstrip("/") + "/interno/chave-anthropic"
        self._token_servico = token_servico

    async def chave_do_cliente(self, autorizacao_usuario: str) -> str | None:
        try:
            resp = await self._http.get(self._url, headers={
                "Authorization": f"Bearer {self._token_servico}", CABECALHO_USUARIO: autorizacao_usuario,
            })
        except httpx.HTTPError as e:
            log.error("Falha ao buscar a chave própria: %s", type(e).__name__)
            return None
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            # Só o status: o corpo de uma rota de segredo não vai para log.
            log.error("Chave própria indisponível: HTTP %s", resp.status_code)
            return None
        chave = resp.json().get("chave")
        return chave if isinstance(chave, str) and chave else None
