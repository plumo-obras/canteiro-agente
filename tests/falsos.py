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


@dataclass
class Chamada:
    metodo: str
    caminho: str
    cabecalhos: dict[str, str]
    corpo: Any
    params: dict[str, str] = field(default_factory=dict)


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

    def __call__(self, req: httpx.Request) -> httpx.Response:
        corpo = json.loads(req.content) if req.content else None
        self.chamadas.append(Chamada(req.method, req.url.path, dict(req.headers), corpo, dict(req.url.params)))
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
        if caminho == f"/obras/{OBRA_EMEI}/orcamento":
            return httpx.Response(200, json=ORCAMENTO_EMEI)
        if caminho == f"/obras/{OBRA_EMEI}/cronograma":
            return httpx.Response(200, json=CRONOGRAMA_EMEI)
        if caminho == f"/obras/{OBRA_EMEI}/resumo":
            return httpx.Response(200, json=RESUMO_EMEI)
        if caminho == f"/obras/{OBRA}/resumo":
            return httpx.Response(200, json=RESUMO_VILA)
        if caminho == f"/obras/{OBRA}/tributos":
            return httpx.Response(200, json=TRIBUTOS_VILA)
        if caminho == "/propostas" and m == "POST":
            return self._propor(corpo)
        if caminho == "/propostas" and m == "GET":
            st, obra = req.url.params.get("status"), req.url.params.get("obra_id")
            return httpx.Response(200, json=[p for p in reversed(self.propostas.values())
                                             if (not st or p["status"] == st) and (not obra or p["obra_id"] == obra)])
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
# saldo, estourou e aditivos_pct como a API calcula (servicos/versoes.py): o agente só os formata. O gasto são os
# comprovantes confirmados.
_ETAPA = lambda e, d, c, a, gasto: {"etapa": e, "descricao": d, "contratado": c, "aditivos": a,  # noqa: E731
                                    "atual": c + a, "gasto": gasto,
                                    "saldo": c + a - gasto, "estourou": gasto > c + a}
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
    "etapas": [_ETAPA("1", "SERVIÇOS PRELIMINARES", 6117367, 2035614, 0),
               _ETAPA("3", "FUNDAÇÕES", 10588514, 1036168, 11_800_000)],
    "totais": {"contratado": 203500158, "aditivos": 5983180, "atual": 209483338, "n_itens": 496, "n_etapas": 24,
               "gasto": 11_830_000, "saldo": 209483338 - 11_830_000,
               "aditivos_pct": 294,
               "sem_etapa": {"gasto": 0},
               # os gastos por comprovante, como a API repartiu: já somados no gasto dos totais
               "gastos": {"total": 11_830_000, "nas_etapas": 11_800_000, "fora_do_orcamento": 30_000,
                          "na_lista_padrao": 0, "sem_etapa_do_orcamento": 0}},
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

def _obra(id_, nome, cidade, status="em_andamento"):
    return {"id": id_, "tenant_id": "t1", "nome": nome, "cidade": cidade, "status": status, "verba": 0}


def _no_mes(etapa, descricao, pct, valor, previsto, medido, situacao, comeca=False):
    return {"etapa": etapa, "descricao": descricao, "pct_no_mes": pct, "valor_no_mes": valor,
            "previsto_ate_referencia": previsto, "medido_acumulado": medido, "situacao": situacao, "comeca": comeca}


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
        # como a API devolve desde o 2º teste com o modelo real: maior valor primeiro, `comeca` pela medição
        # (Fundações já têm 4,51% medidos: continuam em dezembro, não começam) e o mês seguinte inteiro
        "etapas_do_mes": [
            _no_mes("1", "SERVIÇOS PRELIMINARES", 10000, 8152981, 10000, 8886, "atrasada"),
            _no_mes("23", "SERVIÇOS COMPLEMENTARES", 1500, 1078387, 1500, 1000, "atrasada"),
            _no_mes("2", "MOVIMENTO DE TERRA", 1500, 999754, 1500, 2483, "adiantada")],
        "proximo_mes": "2026-12", "previsto_no_proximo_mes": 16270792, "pct_previsto_acumulado_no_proximo_mes": 1170,
        "etapas_do_proximo_mes": [
            _no_mes("3", "FUNDAÇÕES", 7000, 7411960, 0, 451, "adiantada"),
            _no_mes("2", "MOVIMENTO DE TERRA", 8500, 5665273, 1500, 2483, "adiantada"),
            _no_mes("23", "SERVIÇOS COMPLEMENTARES", 2500, 1797312, 1500, 1000, "atrasada"),
            _no_mes("4", "SUPERESTRUTURA", 1000, 1129568, 0, 0, "nao_comecou", comeca=True),
            _no_mes("21", "SPDA", 1500, 266679, 0, 0, "nao_comecou", comeca=True)],
        "proximo_mes_com_inicio": "2026-12",
        "proximas_etapas": [_no_mes("4", "SUPERESTRUTURA", 1000, 1129568, 0, 0, "nao_comecou", comeca=True),
                            _no_mes("21", "SPDA", 1500, 266679, 0, 0, "nao_comecou", comeca=True)],
        "etapas_atrasadas": [{"etapa": "1", "descricao": "SERVIÇOS PRELIMINARES", "medido_acumulado": 8886,
                              "previsto_ate_referencia": 10000, "atraso_pct": 1114}],
    },
    "financeiro": {"tem_orcamento": True, "contratado": 203500158, "aditivos": 5983180, "aditivos_pct": 294,
                   "orcado_atual": 209483338, "gasto": 11_830_000,
                   "saldo": 209483338 - 11_830_000,
                   "etapas_estouradas": [{"etapa": "3", "descricao": "FUNDAÇÕES", "saldo": -175_318}]},
    "pendencias": {"propostas": 1, "titulos": ["Cronograma da obra: 24 etapas em 11 meses"]},
}

RESUMO_VILA = {
    "obra": _obra(OBRA, "Casa Vila Nova", "Campinas"),
    "hoje": "2026-11-05",
    "cronograma": {"cadastrado": False, "mes_corrente": "2026-11", "termino_contratual": None,
                   "etapas_do_mes": [], "proximas_etapas": [], "etapas_atrasadas": []},
    "financeiro": {"tem_orcamento": False, "contratado": 0, "aditivos": 0, "aditivos_pct": None,
                   "orcado_atual": 0, "gasto": 0, "saldo": 0,
                   "etapas_estouradas": []},
    "pendencias": {"propostas": 0, "titulos": []},
}


# GET /obras/{id}/tributos da Vila Nova: os números da conta do domínio (canteiro_dominio.tributos), à mão.
TRIBUTOS_VILA = {
    "obra_id": OBRA, "cno": "900123456789", "vau_referencia": "2026-10",
    "vau_usado": 200_000, "vau_fonte": "tabela_sero",
    "afericao": "2026-11", "categoria": "obra_nova", "selic_ate": "2026-10",
    "inss": {"situacao": "calculado", "motivos": [], "area_equivalente": 143_500, "custo_obra": 28_700_000,
             "rmt": 2_296_000, "fator_social": 4000, "percentual_rmt": 2000, "percentual_categoria": 10000,
             "pre_moldado": None, "nao_decadente": 10000, "comprovado": 870_315,
             "comprovado_por_natureza": {"mao_de_obra_nf": 800_000, "concreto_usinado": 70_315},
             "credito_concreto": 70_315, "credito_autonomo": 0, "base": 1_425_685, "inss_sem_fator_ajuste": 524_652,
             "inss": 524_652, "fator_ajuste": "nao_atingido", "fator_ajuste_minimo": 5000,
             "creditos_fator_ajuste": 800_000, "falta_fator_ajuste": 348_000, "economia_com_notas": 147_200,
             "mao_de_obra_sem_nf": 1_000_000, "notas_sem_cno": 0, "selic_faltando": [], "avisos": [],
             "linhas": ["área para cálculo = 150 m² × 89% (equivalência) + 20 m² × 50% = 143,5 m²",
                        "custo da obra = VAU R$ 2.000,00/m² × 143,5 m² = R$ 287.000,00",
                        "mão de obra presumida (RMT) = R$ 287.000,00 × 20% (alvenaria) × fator social 40% = "
                        "R$ 22.960,00",
                        "crédito do concreto usinado = R$ 287.000,00 × 4,9% (Anexo I, SP) × 5% = R$ 703,15",
                        "remuneração comprovada (corrigida pela Selic) = R$ 8.000,00",
                        "base = R$ 22.960,00 − R$ 8.703,15 = R$ 14.256,85",
                        "INSS = R$ 14.256,85 × 36,8% = R$ 5.246,52",
                        "fator de ajuste: faltam R$ 3.480,00 de remuneração declarada para chegar a 50% da RMT e "
                        "zerar o INSS",
                        "R$ 10.000,00 de mão de obra sem nota (ou sem o CNO na nota): comprovada, o INSS cairia "
                        "R$ 1.472,00"]},
    "ibs_cbs": {"situacao": "nao_incide", "aliquota_efetiva": 0, "valor_operacao": 0, "redutor_ajuste": 0,
                "redutor_social": 0, "base": 0, "debito": 0, "creditos": 30_000, "a_pagar": 0,
                "motivos": ["Obra para uso próprio não é venda nem prestação de serviço: não há IBS/CBS sobre ela."],
                "transicao": "2026 é ano de teste: CBS 0,9% e IBS 0,1% destacados na nota e compensáveis.",
                "linhas": []},
    "a_classificar": 1, "valor_a_classificar": 70_000,
    "sem_nota": [{"id": "g1", "valor": 1_000_000, "data_pagamento": "2026-10-30", "favorecido": "Zé Empreiteiro",
                  "descricao": "alvenaria do térreo", "natureza_fiscal": "mao_de_obra_sem_nf"}],
    "pendentes": [{"id": "g2", "valor": 70_000, "data_pagamento": "2026-11-02", "favorecido": "Elétrica Luz",
                   "descricao": "fios", "natureza_fiscal": None}],
    "parametros": {"inss": {}, "ibs_cbs": {}},
    "aviso": "Estimativa feita com os dados da obra; confira com o contador antes de pagar ou declarar.",
}
