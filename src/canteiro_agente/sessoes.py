"""Histórico da conversa por `sessao_id`.

Produção: rotas `/sessoes` da API central (um documento por mensagem, portável Mongo → Firestore), chamadas com o
token do usuário: a API garante que a sessão é de quem a criou. Testes: a mesma interface em memória.

Só se acrescenta. Reescrever mensagem antiga invalidaria o cache do prefixo e os blocos de raciocínio do modelo, e a
API recusa (409) qualquer lote que não comece exatamente no total de mensagens já gravadas.
"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from canteiro_dominio.tipos import novo_id

from .api_central import ErroApi, mensagem_de

LOTE_MAX = 50          # limite da API por POST de mensagens
PAGINA = 200


class SessaoDeOutro(PermissionError):
    pass


class HistoricoDivergiu(RuntimeError):
    """Outra aba/instância gravou nesta sessão ao mesmo tempo (409): este lote não entrou."""


@dataclass
class Sessao:
    id: str
    dono: str
    mensagens: list[dict[str, Any]] = field(default_factory=list)   # formato da Anthropic: {role, content}
    total: int = 0                                                   # quantas já estão gravadas


class RepositorioSessoes(Protocol):
    async def abrir(
        self, sessao_id: str | None, *, dono: str, autorizacao: str, obra_id: str | None = None, titulo: str = ""
    ) -> Sessao: ...

    async def acrescentar(self, sessao: Sessao, novas: list[dict[str, Any]], *, autorizacao: str) -> None: ...


def _para_api(m: dict[str, Any]) -> dict[str, Any]:
    return {"papel": m["role"], "conteudo": m["content"]}


def _da_api(m: dict[str, Any]) -> dict[str, Any]:
    return {"role": m["papel"], "content": m["conteudo"]}


class SessoesApi:
    def __init__(self, http: httpx.AsyncClient, base_url: str):
        self._http = http
        self._base = base_url.rstrip("/")

    async def _req(self, metodo: str, caminho: str, autorizacao: str, **kw: Any) -> Any:
        resp = await self._http.request(metodo, self._base + caminho, headers={"Authorization": autorizacao}, **kw)
        if resp.status_code in (403, 404):
            # A API responde 404 para sessão de outra pessoa: não revela que ela existe.
            raise SessaoDeOutro("Conversa não encontrada.")
        if resp.status_code == 409:
            raise HistoricoDivergiu(mensagem_de(resp))
        if resp.status_code >= 400:
            raise ErroApi(resp.status_code, mensagem_de(resp))
        return resp.json()

    async def abrir(
        self, sessao_id: str | None, *, dono: str, autorizacao: str, obra_id: str | None = None, titulo: str = ""
    ) -> Sessao:
        corpo: dict[str, Any] = {"id": sessao_id or novo_id(), "titulo": titulo}
        if obra_id:
            corpo["obra_id"] = obra_id
        s = await self._req("POST", "/sessoes", autorizacao, json=corpo)
        mensagens: list[dict[str, Any]] = []
        depois_de = -1
        while len(mensagens) < s["total_mensagens"]:
            pagina = await self._req(
                "GET", f"/sessoes/{s['id']}/mensagens", autorizacao, params={"depois_de": depois_de, "limite": PAGINA}
            )
            if not pagina:
                break
            mensagens += [_da_api(m) for m in pagina]
            depois_de = pagina[-1]["seq"]
        return Sessao(s["id"], s.get("dono", dono), mensagens, s["total_mensagens"])

    async def acrescentar(self, sessao: Sessao, novas: list[dict[str, Any]], *, autorizacao: str) -> None:
        for i in range(0, len(novas), LOTE_MAX):
            lote = novas[i:i + LOTE_MAX]
            await self._req(
                "POST", f"/sessoes/{sessao.id}/mensagens", autorizacao,
                json={"seq_inicial": sessao.total, "mensagens": [_para_api(m) for m in lote]},
            )
            sessao.mensagens += lote
            sessao.total += len(lote)


class SessoesEmMemoria:
    """Para testes e desenvolvimento sem API. Guarda as últimas `limite` sessões."""

    def __init__(self, limite: int = 1000):
        self._dados: OrderedDict[str, Sessao] = OrderedDict()
        self._limite = limite
        self._trava = asyncio.Lock()

    async def abrir(
        self, sessao_id: str | None, *, dono: str, autorizacao: str, obra_id: str | None = None, titulo: str = ""
    ) -> Sessao:
        async with self._trava:
            if sessao_id and sessao_id in self._dados:
                s = self._dados[sessao_id]
                if s.dono != dono:
                    raise SessaoDeOutro("Conversa não encontrada.")
                return Sessao(s.id, s.dono, list(s.mensagens), s.total)
            s = Sessao(sessao_id or novo_id(), dono)
            self._dados[s.id] = s
            while len(self._dados) > self._limite:
                self._dados.popitem(last=False)
            return Sessao(s.id, s.dono)

    async def acrescentar(self, sessao: Sessao, novas: list[dict[str, Any]], *, autorizacao: str) -> None:
        async with self._trava:
            guardada = self._dados[sessao.id]
            if guardada.total != sessao.total:
                raise HistoricoDivergiu(f"esperado seq_inicial={guardada.total}")
            guardada.mensagens += novas
            guardada.total += len(novas)
            self._dados.move_to_end(sessao.id)
        sessao.mensagens += novas
        sessao.total += len(novas)
