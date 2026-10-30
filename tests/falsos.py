"""Dublês dos dois lados do agente, sem rede e sem gastar token.

- `ApiFalsa`: a API central conforme docs/contrato.md, com o seed da Casa Vila Nova, via `httpx.MockTransport`.
- `AnthropicFalsa`: responde `POST /v1/messages` com eventos SSE roteirizados. O cliente `AsyncAnthropic` e o Tool
  Runner são os de verdade; só o transporte é falso. Assim o teste cobre o loop real do SDK (chamar ferramenta,
  devolver tool_result, seguir) e não uma imitação dele.
"""
import base64
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import httpx2
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from canteiro_agente.modelo_roteiro import Resposta, eventos_sse, texto  # noqa: F401
from canteiro_agente.modelo_roteiro import ferramenta as _ferramenta



def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


# JWT no formato do login da API (claims sub/tid); a assinatura é falsa porque quem confere é a API (aqui, a falsa).
TOKEN_USUARIO = f"Bearer {_b64({'alg': 'HS256'})}.{_b64({'sub': 'u1', 'tid': 't1', 'exp': 9999999999})}.assinatura"
TOKEN_OUTRO = f"Bearer {_b64({'alg': 'HS256'})}.{_b64({'sub': 'u2', 'tid': 't1', 'exp': 9999999999})}.assinatura"
USUARIOS = {TOKEN_USUARIO: "u1", TOKEN_OUTRO: "u2"}
TOKEN_SERVICO = "token-de-servico"
OBRA = "obra-vila-nova"
ACO = "insumo-aco"
CIMENTO = "insumo-cimento"
BLOCO = "insumo-bloco"
PED_ACO = "pedido-0158"


@dataclass
class Chamada:
    metodo: str
    caminho: str
    cabecalhos: dict[str, str]
    corpo: Any


@dataclass
class ApiFalsa:
    creditos_status: int = 200
    creditos: dict[str, Any] = field(
        default_factory=lambda: {"saldo_microdolares": 5_000_000, "franquia": 10_000_000, "chave_propria": False}
    )
    chamadas: list[Chamada] = field(default_factory=list)
    sessoes: dict[str, dict[str, Any]] = field(default_factory=dict)
    chave_cliente: str | None = None

    def _insumos(self) -> list[dict[str, Any]]:
        def conta(precisa, saldo, a_caminho, cotando, falta):
            return {"precisa": precisa, "saldo": saldo, "a_caminho": a_caminho, "cotando": cotando,
                    "transferencia": 0, "falta": falta, "comprar": falta}
        return [
            {"insumo": {"id": ACO, "tenant_id": "t1", "obra_id": OBRA, "nome": "Aço CA-50", "unidade": "kg",
                        "curva": "A", "precisa_ate_o_fim": 3_940_000},
             "conta": conta(3_940_000, 140_000, 3_200_000, 600_000, 0)},
            {"insumo": {"id": CIMENTO, "tenant_id": "t1", "obra_id": OBRA, "nome": "Cimento CP-II", "unidade": "sc",
                        "curva": "A", "precisa_ate_o_fim": 200_000},
             "conta": conta(200_000, 46_000, 120_000, 0, 34_000)},
            {"insumo": {"id": BLOCO, "tenant_id": "t1", "obra_id": OBRA, "nome": "Bloco cerâmico 14x19x29",
                        "unidade": "un", "curva": "A", "precisa_ate_o_fim": 4_800_000},
             "conta": conta(4_800_000, 2_760_000, 0, 0, 2_040_000)},
        ]

    def _movimentos(self) -> list[dict[str, Any]]:
        m = lambda tipo, q, destino=None, doc=None: {"tipo": tipo, "quantidade": q, "destino": destino,  # noqa: E731
                                                     "origem_doc": doc, "quem": "u1", "origem": "tela"}
        return [m("saida", 190_000, "Quebra no descarregamento"), m("saida", 1_050_000, "Alvenaria do térreo"),
                m("entrada", 4_000_000, doc="NF 4471")]

    def _pedidos(self) -> list[dict[str, Any]]:
        return [{"id": PED_ACO, "numero": "PED-0158", "obra_id": OBRA, "insumo_id": ACO, "quantidade": 3_200_000,
                 "fornecedor": "Gerdau", "valor": 2_048_000, "status": "em_transito", "previsao": "2026-11-03"}]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        corpo = json.loads(req.content) if req.content else None
        self.chamadas.append(Chamada(req.method, req.url.path, dict(req.headers), corpo))
        caminho, m = req.url.path, req.method
        if caminho == "/uso" and m == "POST":
            return httpx.Response(201, json=corpo)
        if caminho == "/interno/chave-anthropic":
            if req.headers.get("authorization") != f"Bearer {TOKEN_SERVICO}" or \
                    req.headers.get("x-canteiro-usuario") not in USUARIOS:
                return httpx.Response(401, json={"detail": "só o serviço do agente"})
            if not self.chave_cliente:
                return httpx.Response(404, json={"detail": "sem chave própria"})
            return httpx.Response(200, json={"chave": self.chave_cliente}, headers={"Cache-Control": "no-store"})
        usuario = USUARIOS.get(req.headers.get("authorization", ""))
        if usuario is None:
            return httpx.Response(401, json={"detail": "token inválido"})
        if caminho.startswith("/sessoes"):
            return self._sessoes(m, caminho, usuario, corpo, req.url.params)
        if caminho == "/creditos":
            if self.creditos_status == 402:
                return httpx.Response(402, json={"detail": "Os créditos de IA do mês acabaram."})
            return httpx.Response(self.creditos_status, json=self.creditos)
        if caminho == "/obras":
            return httpx.Response(200, json=[{"id": OBRA, "nome": "Casa Vila Nova", "cidade": "Campinas",
                                              "status": "em_andamento", "verba": 48_000_000}])
        if caminho == f"/obras/{OBRA}/insumos":
            return httpx.Response(200, json=self._insumos())
        if caminho == f"/obras/{OBRA}/insumos/{BLOCO}/movimentos":
            return httpx.Response(200, json=self._movimentos()[: int(req.url.params.get("limite", 50))])
        if caminho == f"/obras/{OBRA}/pedidos":
            status = req.url.params.get("status")
            return httpx.Response(200, json=[p for p in self._pedidos() if not status or p["status"] == status])
        if caminho == "/propostas" and m == "POST":
            return httpx.Response(201, json={"id": "proposta-1", "tenant_id": "t1", "status": "pendente",
                                             "sugerido_por": "agente", **corpo})
        return httpx.Response(404, json={"detail": f"rota falsa não conhece {m} {caminho}"})

    def _sessoes(self, m: str, caminho: str, usuario: str, corpo: Any, params: Any) -> httpx.Response:
        """Como a API: um documento por mensagem, só acrescenta, sessão de outra pessoa = 404."""
        partes = caminho.strip("/").split("/")
        if m == "POST" and len(partes) == 1:
            s = self.sessoes.get(corpo["id"])
            if s is None:
                s = self.sessoes[corpo["id"]] = {"id": corpo["id"], "dono": usuario, "obra_id": corpo.get("obra_id"),
                                                 "titulo": corpo.get("titulo", ""), "msgs": []}
                codigo = 201
            elif s["dono"] != usuario:
                return httpx.Response(404, json={"detail": "sessão não encontrada"})
            else:
                codigo = 200
            return httpx.Response(codigo, json=self._sessao(s))
        s = self.sessoes.get(partes[1])
        if s is None or s["dono"] != usuario:
            return httpx.Response(404, json={"detail": "sessão não encontrada"})
        if m == "GET":
            depois_de, limite = int(params.get("depois_de", -1)), int(params.get("limite", 200))
            return httpx.Response(200, json=[x for x in s["msgs"] if x["seq"] > depois_de][:limite])
        if len(corpo["mensagens"]) > 50:
            return httpx.Response(422, json={"detail": "até 50 mensagens"})
        novas = [{"id": f"{s['id']}-{corpo['seq_inicial'] + i:06d}", "sessao_id": s["id"],
                  "seq": corpo["seq_inicial"] + i, **x} for i, x in enumerate(corpo["mensagens"])]
        if corpo["seq_inicial"] != len(s["msgs"]):
            ja = s["msgs"][corpo["seq_inicial"]:corpo["seq_inicial"] + len(novas)]
            if [(x["papel"], x["conteudo"]) for x in ja] == [(x["papel"], x["conteudo"]) for x in novas]:
                return httpx.Response(200, json=self._sessao(s))
            return httpx.Response(409, json={"detail": f"seq_inicial esperado: {len(s['msgs'])}"})
        s["msgs"] += novas
        return httpx.Response(201, json=self._sessao(s))

    @staticmethod
    def _sessao(s: dict[str, Any]) -> dict[str, Any]:
        return {"id": s["id"], "tenant_id": "t1", "dono": s["dono"], "obra_id": s["obra_id"], "titulo": s["titulo"],
                "abas": [], "total_mensagens": len(s["msgs"])}

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def feitas(self, metodo: str, caminho: str) -> list[Chamada]:
        return [c for c in self.chamadas if c.metodo == metodo and c.caminho == caminho]


# ---------------------------------------------------------------- Anthropic

def ferramenta(nome: str, entrada: dict[str, Any], id_: str = "toolu_1") -> dict[str, Any]:
    return _ferramenta(nome, entrada, id_)


@dataclass
class AnthropicFalsa:
    roteiro: list[Resposta]
    pedidos: list[dict[str, Any]] = field(default_factory=list)
    cabecalhos: list[dict[str, str]] = field(default_factory=list)
    chaves: list[str] = field(default_factory=list)

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.pedidos.append(json.loads(req.content))
        self.cabecalhos.append(dict(req.headers))
        self.chaves.append(req.headers.get("x-api-key") or req.headers.get("authorization", "").removeprefix("Bearer "))
        if not self.roteiro:
            return httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "roteiro acabou"}})
        r = self.roteiro.pop(0)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=eventos_sse(r, len(self.pedidos)).encode())

    def cliente(self, chave: str = "chave-falsa") -> AsyncAnthropic:
        return AsyncAnthropic(
            api_key=chave, max_retries=0,
            http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self)),
        )
