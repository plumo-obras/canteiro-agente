"""Cliente da API central. Toda chamada de dado de cliente vai com o token de quem está conversando.

O agente não tem credencial própria para ler ou escrever dado de cliente: tenant e papel são resolvidos pela API a
cada requisição a partir desse token. O token de serviço só serve para gravar o consumo (`POST /uso`).
"""
import re
from typing import Any
from urllib.parse import quote

import httpx

# Cabeçalho com o token do usuário no `POST /uso`: a API tira tenant e usuário dele (no servidor), em vez de confiar
# num tenant_id mandado pelo agente.
CABECALHO_USUARIO = "X-Canteiro-Usuario"


class ErroApi(Exception):
    def __init__(self, status: int, mensagem: str):
        super().__init__(mensagem)
        self.status = status
        self.mensagem = mensagem


FORA_DO_AR = "O sistema está fora do ar agora. Tente de novo em instantes."
# O que o proxy da hospedagem responde enquanto a API está caída ou reiniciando. A API em si não usa nenhum deles.
STATUS_FORA_DO_AR = frozenset({502, 503, 504})


class ApiForaDoAr(ErroApi):
    """A API não respondeu (conexão recusada, tempo esgotado) ou o proxy dela avisou que ela caiu.

    É um `ErroApi` para as ferramentas tratarem no mesmo `except`; o `detalhe` (nome do erro de rede) é só para o
    log. Ninguém mostra ao modelo o texto cru da exceção do httpx."""

    def __init__(self, detalhe: str):
        super().__init__(503, FORA_DO_AR)
        self.detalhe = detalhe


_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _id(valor: Any) -> str:
    """Segmento de URL vindo do modelo. Sem isto, um id como "x/../../interno/…" (injeção de instrução escondida num
    dado da obra) levaria a ferramenta a ler outra rota da API com o token do usuário."""
    texto = str(valor)
    if not _ID.fullmatch(texto):
        raise ErroApi(400, f"Identificador inválido: {texto[:40]!r}.")
    return quote(texto, safe="")


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
        # Uma instância por volta. Vira True na primeira falha de rede: a ferramenta devolve erro ao modelo (o SDK
        # transforma qualquer exceção da ferramenta em tool_result), e a volta lê isto para parar em vez de pagar
        # mais uma chamada ao modelo que só tentaria de novo contra a API caída.
        self.fora_do_ar = False

    async def _req(self, metodo: str, caminho: str, **kw: Any) -> Any:
        try:
            resp = await self._http.request(
                metodo, self._base + caminho, headers={"Authorization": self._autorizacao}, **kw
            )
        except httpx.HTTPError as e:
            self.fora_do_ar = True
            raise ApiForaDoAr(type(e).__name__) from None
        if resp.status_code in STATUS_FORA_DO_AR:
            self.fora_do_ar = True
            raise ApiForaDoAr(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise ErroApi(resp.status_code, mensagem_de(resp))
        return resp.json() if resp.content else None

    async def creditos(self) -> dict[str, Any]:
        return await self._req("GET", "/creditos")

    async def obras(self) -> list[dict[str, Any]]:
        return await self._req("GET", "/obras")

    async def insumos(self, obra_id: str) -> list[dict[str, Any]]:
        return await self._req("GET", f"/obras/{_id(obra_id)}/insumos")

    async def movimentos(self, obra_id: str, insumo_id: str, limite: int) -> list[dict[str, Any]]:
        return await self._req(
            "GET", f"/obras/{_id(obra_id)}/insumos/{_id(insumo_id)}/movimentos", params={"limite": limite}
        )

    async def pedidos(self, obra_id: str, status: str | list[str] | None = None) -> list[dict[str, Any]]:
        # lista vira ?status=a&status=b, que a API lê como "in"
        params = {"status": status} if status else None
        return await self._req("GET", f"/obras/{_id(obra_id)}/pedidos", params=params)

    async def resumo_obra(self, obra_id: str) -> dict[str, Any]:
        return await self._req("GET", f"/obras/{_id(obra_id)}/resumo")

    async def orcamento(self, obra_id: str) -> dict[str, Any]:
        return await self._req("GET", f"/obras/{_id(obra_id)}/orcamento")

    async def etapas(self, obra_id: str) -> list[dict[str, Any]]:
        return await self._req("GET", f"/obras/{_id(obra_id)}/etapas")

    async def cronograma(self, obra_id: str) -> dict[str, Any]:
        return await self._req("GET", f"/obras/{_id(obra_id)}/cronograma")

    async def fornecedores(self) -> list[dict[str, Any]]:
        return await self._req("GET", "/fornecedores")

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
