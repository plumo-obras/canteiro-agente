"""Cliente da API central. Toda chamada de dado de cliente vai com o token de quem está conversando.

O agente não tem credencial própria para ler ou escrever dado de cliente: tenant e papel são resolvidos pela API a
cada requisição a partir desse token. O token de serviço só serve para gravar o consumo (`POST /uso`).
"""
from typing import Any

import httpx

# Cabeçalho com o token do usuário no `POST /uso`: a API tira tenant e usuário dele (no servidor), em vez de confiar
# num tenant_id mandado pelo agente.
CABECALHO_USUARIO = "X-Canteiro-Usuario"


class ErroApi(Exception):
    def __init__(self, status: int, mensagem: str):
        super().__init__(mensagem)
        self.status = status
        self.mensagem = mensagem


def mensagem_de(resp: httpx.Response) -> str:
    try:
        corpo = resp.json()
    except ValueError:
        return resp.text[:300] or f"erro {resp.status_code}"
    if isinstance(corpo, dict):
        detalhe = corpo.get("mensagem") or corpo.get("detail") or corpo.get("erro")
        if isinstance(detalhe, str):
            return detalhe
        if detalhe is not None:
            return str(detalhe)[:300]
    return f"erro {resp.status_code}"


class ApiCentral:
    def __init__(self, http: httpx.AsyncClient, base_url: str, autorizacao: str):
        # `autorizacao` é o cabeçalho Authorization do usuário, repassado como veio ("Bearer …").
        self._http = http
        self._base = base_url.rstrip("/")
        self._autorizacao = autorizacao

    async def _req(self, metodo: str, caminho: str, **kw: Any) -> Any:
        resp = await self._http.request(
            metodo, self._base + caminho, headers={"Authorization": self._autorizacao}, **kw
        )
        if resp.status_code >= 400:
            raise ErroApi(resp.status_code, mensagem_de(resp))
        return resp.json() if resp.content else None

    async def creditos(self) -> dict[str, Any]:
        return await self._req("GET", "/creditos")

    async def obras(self) -> list[dict[str, Any]]:
        return await self._req("GET", "/obras")

    async def insumos(self, obra_id: str) -> list[dict[str, Any]]:
        return await self._req("GET", f"/obras/{obra_id}/insumos")

    async def movimentos(self, obra_id: str, insumo_id: str, limite: int) -> list[dict[str, Any]]:
        return await self._req(
            "GET", f"/obras/{obra_id}/insumos/{insumo_id}/movimentos", params={"limite": limite}
        )

    async def pedidos(self, obra_id: str, status: str | None = None) -> list[dict[str, Any]]:
        params = {"status": status} if status else None
        return await self._req("GET", f"/obras/{obra_id}/pedidos", params=params)

    async def criar_proposta(self, corpo: dict[str, Any]) -> dict[str, Any]:
        return await self._req("POST", "/propostas", json=corpo)


async def registrar_uso(
    http: httpx.AsyncClient, base_url: str, token_servico: str, autorizacao_usuario: str, uso: dict[str, Any]
) -> None:
    """`POST /uso` com o token de serviço. O `id` vem no corpo: repetir a chamada não duplica a linha."""
    resp = await http.post(
        base_url.rstrip("/") + "/uso",
        json=uso,
        headers={"Authorization": f"Bearer {token_servico}", CABECALHO_USUARIO: autorizacao_usuario},
    )
    if resp.status_code >= 400:
        raise ErroApi(resp.status_code, mensagem_de(resp))
