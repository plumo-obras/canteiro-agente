"""Dublês dos dois lados do agente, sem rede e sem gastar token.

- `ApiFalsa`: a API central conforme docs/contrato.md, com o seed da Casa Vila Nova, via `httpx.MockTransport`.
- `AnthropicFalsa`: responde `POST /v1/messages` com eventos SSE roteirizados. O cliente `AsyncAnthropic` e o Tool
  Runner são os de verdade; só o transporte é falso. Assim o teste cobre o loop real do SDK (chamar ferramenta,
  devolver tool_result, seguir) e não uma imitação dele.
"""
import asyncio
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
PED_CIMENTO = "pedido-0160"


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
    propostas: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Simula a API caída: a partir de `fora_do_ar_depois` requisições de dado (fora /creditos e /sessoes), o
    # transporte falha como um ConnectError (None = nunca).
    fora_do_ar_depois: int | None = None

    def _insumos(self) -> list[dict[str, Any]]:
        # `texto` é o que a API monta com regras.texto_da_conta (aqui escrito à mão: o dublê não faz conta)
        def conta(precisa, saldo, a_caminho, cotando, falta, texto):
            return {"precisa": precisa, "saldo": saldo, "a_caminho": a_caminho, "cotando": cotando,
                    "transferencia": 0, "falta": falta, "comprar": falta, "sobra": 0, "sem_previsao": False,
                    "texto": texto}
        return [
            {"insumo": {"id": ACO, "tenant_id": "t1", "obra_id": OBRA, "nome": "Aço CA-50", "unidade": "kg",
                        "curva": "A", "precisa_ate_o_fim": 3_940_000},
             "conta": conta(3_940_000, 140_000, 3_200_000, 600_000, 0,
                            "precisa 3.940 kg − saldo 140 kg − a caminho 3.200 kg − em cotação 600 kg − "
                            "transferência 0 kg = falta 0 kg"),
             "ultimo_preco": None},
            {"insumo": {"id": CIMENTO, "tenant_id": "t1", "obra_id": OBRA, "nome": "Cimento CP-II", "unidade": "sc",
                        "curva": "A", "precisa_ate_o_fim": 200_000},
             "conta": conta(200_000, 46_000, 120_000, 0, 34_000,
                            "precisa 200 sc − saldo 46 sc − a caminho 120 sc − em cotação 0 sc − transferência 0 sc"
                            " = falta 34 sc"),
             "ultimo_preco": {"valor_unitario": 3800, "pedido_id": PED_CIMENTO, "numero": "PED-0160",
                              "fornecedor": "Depósito São Jorge", "data": "2026-10-20", "status": "recebido"}},
            {"insumo": {"id": BLOCO, "tenant_id": "t1", "obra_id": OBRA, "nome": "Bloco cerâmico 14x19x29",
                        "unidade": "un", "curva": "A", "precisa_ate_o_fim": 4_800_000},
             "conta": conta(4_800_000, 2_760_000, 0, 0, 2_040_000,
                            "precisa 4.800 un − saldo 2.760 un − a caminho 0 un − em cotação 0 un − transferência "
                            "0 un = falta 2.040 un"),
             "ultimo_preco": None},
        ]

    def _movimentos(self) -> list[dict[str, Any]]:
        m = lambda tipo, q, destino=None, doc=None: {"tipo": tipo, "quantidade": q, "destino": destino,  # noqa: E731
                                                     "origem_doc": doc, "quem": "Joana (mestre)", "origem": "tela"}
        return [m("saida", 190_000, "Quebra no descarregamento"), m("saida", 1_050_000, "Alvenaria do térreo"),
                m("entrada", 4_000_000, doc="NF 4471")]

    def _pedidos(self) -> list[dict[str, Any]]:
        def linha(insumo, nome, un, q, total, recebida=0):
            # falta_entregar: a conta da API (regras.falta_na_linha), que o agente só formata
            return {"insumo_id": insumo, "nome": nome, "unidade": un, "tipo": "material", "etapa": None,
                    "quantidade": q, "valor_unitario": 0, "total": total, "recebida": recebida,
                    "falta_entregar": max(0, q - recebida)}
        return [{"id": PED_ACO, "numero": "PED-0158", "obra_id": OBRA, "fornecedor": "Gerdau", "valor": 2_048_000,
                 "status": "em_transito", "previsao": "2026-11-03", "recebimentos": 0,
                 "linhas": [linha(ACO, "Aço CA-50", "kg", 3_200_000, 2_048_000)]},
                {"id": PED_CIMENTO, "numero": "PED-0160", "obra_id": OBRA, "fornecedor": "Depósito São Jorge",
                 "valor": 456_000, "status": "recebido", "previsao": "2026-11-01", "recebimentos": 0,
                 "linhas": [linha(CIMENTO, "Cimento CP-II", "sc", 120_000, 456_000)]}]

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
        if self.fora_do_ar_depois is not None and caminho != "/creditos" and not caminho.startswith("/sessoes"):
            if self.fora_do_ar_depois <= 0:
                raise httpx.ConnectError("All connection attempts failed", request=req)
            self.fora_do_ar_depois -= 1
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
            status = req.url.params.get_list("status")
            return httpx.Response(200, json=[p for p in self._pedidos() if not status or p["status"] in status])
        if caminho == f"/obras/{OBRA_EMEI}/orcamento":
            return httpx.Response(200, json=ORCAMENTO_EMEI)
        if caminho == f"/obras/{OBRA_EMEI}/cronograma":
            return httpx.Response(200, json=CRONOGRAMA_EMEI)
        if caminho == f"/obras/{OBRA_EMEI}/etapas":
            return httpx.Response(200, json=[{"etapa": "1", "descricao": "SERVIÇOS PRELIMINARES"},
                                             {"etapa": "3", "descricao": "FUNDAÇÕES"}])
        if caminho == f"/obras/{OBRA_EMEI}/insumos":
            return httpx.Response(200, json=INSUMOS_EMEI)
        if caminho == f"/obras/{OBRA_EMEI}/pedidos":
            status = req.url.params.get_list("status")
            return httpx.Response(200, json=[p for p in PEDIDOS_EMEI if not status or p["status"] in status])
        if caminho == f"/obras/{OBRA_EMEI}/resumo":
            return httpx.Response(200, json=RESUMO_EMEI)
        if caminho == f"/obras/{OBRA}/resumo":
            return httpx.Response(200, json=RESUMO_VILA)
        if caminho == "/fornecedores":
            return httpx.Response(200, json=[{"id": "f1", "nome": "Depósito São Jorge", "cnpj": "11222333000181",
                                              "contato": "Jorge", "telefone": "51 9999", "email": ""}])
        if caminho == f"/obras/{OBRA}/etapas":
            return httpx.Response(200, json=[])          # a Vila Nova da demonstração não tem orçamento em etapas
        if caminho == "/propostas" and m == "POST":
            return self._propor(corpo)
        return httpx.Response(404, json={"detail": f"rota falsa não conhece {m} {caminho}"})

    def _propor(self, corpo: dict[str, Any]) -> httpx.Response:
        """Como a API: a mesma chave devolve a mesma proposta; igual (obra, tipo, dados) pendente também."""
        chave = corpo.get("chave_idempotencia")
        corpo = {k: v for k, v in corpo.items() if k != "chave_idempotencia"}
        id_ = f"proposta-{chave}" if chave else f"proposta-{len(self.propostas) + 1}"
        existente = self.propostas.get(id_) or next(
            (p for p in self.propostas.values() if p["status"] == "pendente"
             and (p["obra_id"], p["tipo"], p["dados"]) == (corpo["obra_id"], corpo["tipo"], corpo["dados"])), None)
        if existente:
            return httpx.Response(200, json={**existente, "ja_existia": True})
        p = self.propostas[id_] = {"id": id_, "tenant_id": "t1", "status": "pendente", "sugerido_por": "agente",
                                   **corpo}
        return httpx.Response(201, json={**p, "ja_existia": False})

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
class Crua:
    """Resposta montada à mão: pedaços do SSE e, entre eles, pausas (float, em segundos). Serve para o que o roteiro
    normal não faz: conexão que fica parada no meio (cancelamento, tempo esgotado, ping) e JSON quebrado."""
    partes: list[str | float]


def pausada(r: Resposta, segundos: float, antes_de: str = "message_delta") -> Crua:
    """A resposta inteira, mas parada `segundos` antes do evento `antes_de` (por padrão: depois do texto, antes de
    o uso final chegar)."""
    sse = eventos_sse(r, 0)
    i = sse.index(f"event: {antes_de}")
    return Crua([sse[:i], segundos, sse[i:]])


def json_ilegivel(texto_antes: str, entrada_entregue: str = "{,") -> Crua:
    """Texto seguido de uma chamada de ferramenta cujo JSON de entrada (em streaming) o SDK não consegue ler."""
    r = Resposta([texto(texto_antes), ferramenta("listar_obras", {}, "toolu_quebrado")], stop_reason="tool_use")
    sse = eventos_sse(r, 0).replace('"partial_json": "{}"', '"partial_json": ' + json.dumps(entrada_entregue))
    return Crua([sse])


@dataclass
class AnthropicFalsa:
    roteiro: list[Resposta | Crua]
    pedidos: list[dict[str, Any]] = field(default_factory=list)
    cabecalhos: list[dict[str, str]] = field(default_factory=list)
    chaves: list[str] = field(default_factory=list)

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.pedidos.append(json.loads(req.content))
        self.cabecalhos.append(dict(req.headers))
        self.chaves.append(req.headers.get("x-api-key") or req.headers.get("authorization", "").removeprefix("Bearer "))
        if not self.roteiro:
            return httpx2.Response(500, json={"type": "error",
                                              "error": {"type": "api_error", "message": "roteiro acabou"}})
        r = self.roteiro.pop(0)
        if isinstance(r, Crua):
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_corpo(r.partes))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=eventos_sse(r, len(self.pedidos)).encode())

    def cliente(self, chave: str = "chave-falsa") -> AsyncAnthropic:
        return AsyncAnthropic(
            api_key=chave, max_retries=0,
            http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self)),
        )


async def _corpo(partes: list[str | float]):
    for p in partes:
        if isinstance(p, str):
            yield p.encode()
        else:
            await asyncio.sleep(p)


OBRA_EMEI = "obra-emei"
# saldo, estourou e aditivos_pct como a API calcula (servicos/versoes.py): o agente só os formata
_ETAPA = lambda e, d, c, a, cot, comp, real: {"etapa": e, "descricao": d, "contratado": c, "aditivos": a,  # noqa: E731
                                             "atual": c + a, "cotando": cot, "comprometido": comp, "realizado": real,
                                             "saldo": c + a - comp, "estourou": comp > c + a}
ORCAMENTO_EMEI = {
    "versoes": [
        {"id": "v-adit", "tipo": "aditivo", "ativa": True, "numero": 1, "descricao": "Aterro, tapume e aço",
         "data": "2026-11-20", "total": 5983180, "itens": 5},
        {"id": "v-cont", "tipo": "contrato", "ativa": True, "numero": 0, "descricao": "", "data": None,
         "total": 203500158, "itens": 491},
    ],
    "itens": [{"tipo": "item", "numero": "1.2", "etapa": "1", "codigo": "74220/1", "fonte": "SINAPI",
               "descricao": "Tapume de chapa de madeira", "quantidade": 88000, "unidade": "m²",
               "valor_unitario": 4523, "total": 398024, "versao_id": "v-cont"},
              {"tipo": "item", "numero": "A1.2", "etapa": "1", "codigo": "74220/1", "fonte": "SINAPI",
               "descricao": "Tapume de chapa de madeira", "quantidade": 348000, "unidade": "m²",
               "valor_unitario": 4523, "total": 1574004, "versao_id": "v-adit"}],
    "etapas": [_ETAPA("1", "SERVIÇOS PRELIMINARES", 6117367, 2035614, 0, 0, 0),
               _ETAPA("3", "FUNDAÇÕES", 10588514, 1036168, 0, 2425000, 2334000)],
    "totais": {"contratado": 203500158, "aditivos": 5983180, "atual": 209483338, "n_itens": 496, "n_etapas": 24,
               "cotando": 0, "comprometido": 2425000, "realizado": 2334000, "saldo": 209483338 - 2425000,
               "aditivos_pct": 294,
               "sem_etapa": {"cotando": 0, "comprometido": 0, "realizado": 0}},
}
CRONOGRAMA_EMEI = {
    "inicio": "2026-11", "meses": 11, "base": {"inicio": "2026-11", "meses": 10}, "revisoes": 1,
    # como a API devolve (regras.situacao_etapa): 1 atrasada, 2 adiantada, 4 começa em dezembro
    "etapas": [
        {"etapa": "1", "descricao": "SERVIÇOS PRELIMINARES", "atual": 8152981, "percentuais": [10000, 0],
         "medido_acumulado": 8886, "valores_mes": [8152981, 0], "medido_mes": [8886, 0],
         "previsto_ate_referencia": 10000, "situacao": "atrasada", "atraso_pct": 1114},
        {"etapa": "2", "descricao": "MOVIMENTO DE TERRA", "atual": 6665027, "percentuais": [1500, 8500],
         "medido_acumulado": 2483, "valores_mes": [999754, 5665273], "medido_mes": [2483, 0],
         "previsto_ate_referencia": 1500, "situacao": "adiantada", "atraso_pct": 0},
        {"etapa": "4", "descricao": "SUPERESTRUTURA", "atual": 11295680, "percentuais": [0, 10000],
         "medido_acumulado": 0, "valores_mes": [0, 11295680], "medido_mes": [0, 0],
         "previsto_ate_referencia": 0, "situacao": "nao_comecou", "atraso_pct": 0},
        {"etapa": "9", "descricao": "REVESTIMENTOS", "atual": 29974323, "percentuais": [0, 0],
         "medido_acumulado": 0, "valores_mes": [0, 0], "medido_mes": [0, 0],
         "previsto_ate_referencia": 0, "situacao": "nao_comecou", "atraso_pct": 0},
    ],
    "medicoes": [{"numero": 1, "mes": "2026-11", "valor": 7876733, "pct_obra_acumulado": 376, "observacao": ""}],
    "mensal": [{"mes": "2026-11", "previsto": 8505000, "previsto_acumulado": 8505000, "pct_previsto_acumulado": 406,
                "medido": 7876733, "medido_acumulado": 7876733, "pct_medido_acumulado": 376,
                "pct_base_acumulado": 406},
               {"mes": "2026-12", "previsto": 16270792, "previsto_acumulado": 24775792,
                "pct_previsto_acumulado": 1183, "medido": None, "medido_acumulado": None,
                "pct_medido_acumulado": None, "pct_base_acumulado": 1183}],
    "prazo": {"termino_contratual": "2027-09", "termino_projetado": "2027-09", "atraso_meses": 0, "atraso_decimos": 0,
              "pct_previsto_na_medicao": 406, "pct_medido": 376, "mes_referencia": "2026-11",
              "termino_base": "2027-08", "atraso_base_meses": 1, "atraso_base_decimos": 5},
}

# --- EMEI: o almoxarifado e os pedidos do teste com o modelo real (05/11/2026) ---
CIMENTO_EMEI, AREIA_EMEI, BRITA_EMEI = "insumo-cimento-emei", "insumo-areia-emei", "insumo-brita-emei"
PED_BRITA = "pedido-0170"


def _sem_previsao(saldo: int, a_caminho: int, un: str, texto_saldo: str, texto_caminho: str) -> dict:
    return {"precisa": 0, "saldo": saldo, "a_caminho": a_caminho, "cotando": 0, "transferencia": 0, "falta": 0,
            "comprar": 0, "sobra": 0, "sem_previsao": True,
            "texto": f"sem previsão lançada para a obra (o sistema não sabe quanto ela precisa, então não calcula a "
                     f"falta) · saldo {texto_saldo} {un} · a caminho {texto_caminho} {un} · em cotação 0 {un} · "
                     f"transferência 0 {un}"}


INSUMOS_EMEI = [
    {"insumo": {"id": CIMENTO_EMEI, "obra_id": OBRA_EMEI, "nome": "Cimento CP-II 50 kg", "unidade": "sc",
                "curva": "A", "precisa_ate_o_fim": 0, "valor_unitario": 0},
     "conta": _sem_previsao(60_000, 0, "sc", "60", "0"),
     "ultimo_preco": {"valor_unitario": 3750, "pedido_id": "pedido-0169", "numero": "PED-0169",
                      "fornecedor": "Materiais de Construção Vale do Taquari Ltda", "data": "2026-11-03",
                      "status": "conferido"}},
    {"insumo": {"id": AREIA_EMEI, "obra_id": OBRA_EMEI, "nome": "Areia média", "unidade": "m³", "curva": "B",
                "precisa_ate_o_fim": 0},
     "conta": _sem_previsao(10_000, 0, "m³", "10", "0"), "ultimo_preco": None},
    {"insumo": {"id": BRITA_EMEI, "obra_id": OBRA_EMEI, "nome": "Brita 1", "unidade": "m³", "curva": "B",
                "precisa_ate_o_fim": 0},
     "conta": _sem_previsao(18_000, 7_000, "m³", "18", "7"),
     "ultimo_preco": {"valor_unitario": 13000, "pedido_id": PED_BRITA, "numero": "PED-0170",
                      "fornecedor": "Pedreira Lajeado Britagem Ltda", "data": "2026-11-02", "status": "parcial"}},
]

PEDIDOS_EMEI = [
    {"id": PED_BRITA, "numero": "PED-0170", "obra_id": OBRA_EMEI, "fornecedor": "Pedreira Lajeado Britagem Ltda",
     "valor": 325_000, "status": "parcial", "previsao": "2026-11-16", "recebimentos": 1,
     "criado_em": "2026-11-02T13:00:00Z",
     "linhas": [{"insumo_id": BRITA_EMEI, "nome": "Brita 1", "unidade": "m³", "tipo": "material", "etapa": "3",
                 "quantidade": 25_000, "valor_unitario": 13000, "total": 325_000, "recebida": 18_000,
                 "falta_entregar": 7_000}]},
    {"id": "pedido-0169", "numero": "PED-0169", "obra_id": OBRA_EMEI,
     "fornecedor": "Materiais de Construção Vale do Taquari Ltda", "valor": 450_000, "status": "conferido",
     "previsao": "2026-11-03", "recebimentos": 1, "criado_em": "2026-11-01T13:00:00Z",
     "linhas": [{"insumo_id": CIMENTO_EMEI, "nome": "Cimento CP-II 50 kg", "unidade": "sc", "tipo": "material",
                 "etapa": "3", "quantidade": 120_000, "valor_unitario": 3750, "total": 450_000,
                 "recebida": 120_000, "falta_entregar": 0}]},
]


def _obra(id_, nome, cidade, status="em_andamento"):
    return {"id": id_, "tenant_id": "t1", "nome": nome, "cidade": cidade, "status": status, "verba": 0}


# GET /obras/{id}/resumo como a API calcula (servicos/resumo.py)
RESUMO_EMEI = {
    "obra": _obra(OBRA_EMEI, "EMEI Santa Clara do Sul", "Santa Clara do Sul"),
    "hoje": "2026-11-05",
    "cronograma": {
        "cadastrado": True, "mes_corrente": "2026-11", "termino_contratual": "2027-08",
        "termino_projetado": "2027-08", "termino_original": "2027-08", "atraso_decimos": 0,
        "atraso_base_decimos": 0, "revisoes": 0, "mes_ultima_medicao": "2026-11", "medicoes": 1,
        "pct_previsto_na_medicao": 391, "pct_medido": 396, "previsto_no_mes": 8195509,
        "pct_previsto_acumulado_no_mes": 391,
        "etapas_do_mes": [
            {"etapa": "1", "descricao": "SERVIÇOS PRELIMINARES", "pct_no_mes": 10000, "valor_no_mes": 8152981,
             "previsto_ate_referencia": 10000, "medido_acumulado": 8886, "situacao": "atrasada"},
            {"etapa": "2", "descricao": "MOVIMENTO DE TERRA", "pct_no_mes": 1500, "valor_no_mes": 999754,
             "previsto_ate_referencia": 1500, "medido_acumulado": 2483, "situacao": "adiantada"}],
        "proximo_mes_com_inicio": "2026-12",
        "proximas_etapas": [{"etapa": "4", "descricao": "SUPERESTRUTURA", "pct_no_mes": 10000,
                             "valor_no_mes": 11295680, "previsto_ate_referencia": 0, "medido_acumulado": 0,
                             "situacao": "nao_comecou"}],
        "etapas_atrasadas": [{"etapa": "1", "descricao": "SERVIÇOS PRELIMINARES", "medido_acumulado": 8886,
                              "previsto_ate_referencia": 10000, "atraso_pct": 1114}],
    },
    "financeiro": {"tem_orcamento": True, "contratado": 203500158, "aditivos": 5983180, "aditivos_pct": 294,
                   "orcado_atual": 209483338, "cotando": 0, "comprometido": 2425000, "realizado": 2334000,
                   "saldo": 207058338, "etapas_estouradas": []},
    "entregas": {"hoje": [], "atrasadas": [], "proximos_7_dias": [], "sem_previsao": 0, "nf_para_conferir": 0},
    "almoxarifado": {"insumos": 3, "falta_comprar": [], "sem_previsao": 3, "consumo_projetado": False},
    "pendencias": {"propostas": 1, "titulos": ["PED-0170: encerrar sem esperar o resto"]},
}

RESUMO_VILA = {
    "obra": _obra(OBRA, "Casa Vila Nova", "Campinas"),
    "hoje": "2026-11-05",
    "cronograma": {"cadastrado": False, "mes_corrente": "2026-11", "termino_contratual": None,
                   "etapas_do_mes": [], "proximas_etapas": [], "etapas_atrasadas": []},
    "financeiro": {"tem_orcamento": False, "contratado": 0, "aditivos": 0, "aditivos_pct": None,
                   "orcado_atual": 0, "cotando": 0, "comprometido": 0, "realizado": 0, "saldo": 0,
                   "etapas_estouradas": []},
    "entregas": {"hoje": [], "atrasadas": [
        {"pedido_id": PED_ACO, "numero": "PED-0158", "fornecedor": "Gerdau", "status": "em_transito",
         "previsao": "2026-11-03", "itens": [{"nome": "Aço CA-50", "unidade": "kg", "falta_entregar": 3_200_000}]}],
        "proximos_7_dias": [], "sem_previsao": 0, "nf_para_conferir": 1},
    "almoxarifado": {"insumos": 3, "sem_previsao": 0, "consumo_projetado": False, "falta_comprar": [
        {"insumo_id": CIMENTO, "nome": "Cimento CP-II", "unidade": "sc", "falta": 34_000,
         "texto": "precisa 200 sc − saldo 46 sc − a caminho 120 sc − em cotação 0 sc − transferência 0 sc = falta "
                  "34 sc"},
        {"insumo_id": BLOCO, "nome": "Bloco cerâmico 14x19x29", "unidade": "un", "falta": 2_040_000,
         "texto": "precisa 4.800 un − saldo 2.760 un − a caminho 0 un − em cotação 0 un − transferência 0 un = "
                  "falta 2.040 un"}]},
    "pendencias": {"propostas": 0, "titulos": []},
}
