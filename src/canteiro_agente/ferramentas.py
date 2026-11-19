"""As ferramentas do contrato. Cada uma chama a API central com o token do usuário.

Devolvem números já formatados ("R$ 472,00", "88,86%") como a API somou, para o modelo citar sem fazer conta. As de
nível `propoe` só criam proposta (`POST /propostas`): nada é executado sem a aprovação de uma pessoa.

As ferramentas são criadas por requisição (fecham sobre o token daquela conversa), mas nome, descrição e esquema são
sempre os mesmos bytes: é isso que mantém o cache do prefixo (ferramentas + instrução) válido entre conversas.
"""
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, NotRequired, TypedDict
from zoneinfo import ZoneInfo

from anthropic import beta_async_tool
from anthropic.lib.tools import ToolError

from canteiro_dominio.cronograma import EtapaParaPlano, ServicoDoPlano, montar_cronograma_de_etapas
from canteiro_dominio.tipos import fmt_qtd, fmt_reais

from .api_central import ApiCentral, ApiForaDoAr, ErroApi
from .calculo import ErroConta
from .calculo import calcular as _calcular

Nivel = Literal["consulta", "propoe"]
NIVEIS: dict[str, Nivel] = {
    "listar_obras": "consulta",
    "resumo_obra": "consulta",
    "consultar_orcamento": "consulta",
    "consultar_cronograma": "consulta",
    "consultar_tributos": "consulta",
    # não fala com a API: é a calculadora. Fica como consulta porque não escreve nada.
    "calcular": "consulta",
    "propor_cronograma": "propoe",
}
# Ferramentas que não chamam a API (o teste de "só propõe" confere que elas não fazem requisição nenhuma).
LOCAIS = {"calcular"}

FUSO = ZoneInfo("America/Sao_Paulo")
_MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
_SITUACAO = {"concluida": "concluída", "atrasada": "atrasada", "adiantada": "adiantada", "em_dia": "em dia",
             "nao_comecou": "não começou"}


# Lembrete de "uma vez por conversa" (falta de previsão, de cadastro, data estranha num documento). No segundo teste
# com o modelo real, "lance a previsão no almoxarifado" saiu em 13 de 34 respostas e o aviso de data futura em 4
# seguidas: a ferramenta mandava o mesmo aviso a cada consulta e o modelo o repetia. Agora o aviso tem uma chave; a
# primeira vez ele vai em `lembretes`, e daí em diante só a chave, em `lembretes_ja_ditos`. Quem lembra o que já foi
# dito é o código (o histórico da sessão), não o modelo.
NOTA_JA_DITO = "Já dito antes nesta conversa: não repita, a menos que a pessoa pergunte disso."


def _texto_do_resultado(bloco: dict[str, Any]) -> str:
    c = bloco.get("content")
    if isinstance(c, str):
        return c
    return "".join(p.get("text", "") for p in c or [] if isinstance(p, dict))


def lembretes_do_historico(mensagens: list[dict[str, Any]]) -> set[str]:
    """As chaves de lembrete que alguma ferramenta já entregou nesta sessão (lidas dos tool_result guardados)."""
    chaves: set[str] = set()
    for m in mensagens:
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                continue
            texto = _texto_do_resultado(b)
            if '"lembretes"' not in texto:
                continue
            try:
                dado = json.loads(texto)
            except ValueError:
                continue
            if isinstance(dado, dict) and isinstance(dado.get("lembretes"), dict):
                chaves.update(dado["lembretes"])
    return chaves


class EtapaDoCronograma(TypedDict):
    """Decisão de planejamento de uma etapa do orçamento: a equipe alocada e de que etapas ela depende. A DURAÇÃO não
    entra aqui — o sistema calcula pelos serviços da etapa (quantidade × produtividade). O planejador decide equipe e
    sequência; a conta é do domínio."""
    etapa: str
    equipe: int
    depende_de: NotRequired[list[str]]


@dataclass
class Eventos:
    """O que as ferramentas querem mostrar na tela (eventos `ferramenta` e `proposta` do SSE) e o contexto da volta
    que elas não recebem do SDK (sessão e id da chamada).

    Com `emitir`, o evento sai na hora: o `ferramenta` é anunciado quando a ferramenta COMEÇA, e a tela mostra o que
    está em andamento durante uma consulta lenta, em vez de só depois que ela acaba. Sem `emitir` (testes de
    ferramenta), os eventos ficam em `pendentes`.
    """
    emitir: Callable[[tuple[str, dict[str, Any]]], None] | None = None
    pendentes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    sessao_id: str | None = None
    # (nome, tool_use id, entrada) das chamadas da resposta em execução, na ordem do modelo
    chamadas: list[tuple[str, str, Any]] = field(default_factory=list)
    chamada_atual: str | None = None
    # chaves de lembrete já entregues nesta sessão (conversa.py preenche pelo histórico guardado)
    lembretes_dados: set[str] = field(default_factory=set)
    # "hoje" em Brasília, para resolver "chegou hoje" e marcar data futura; fixável nos testes
    hoje: date = field(default_factory=lambda: datetime.now(FUSO).date())

    def com_lembretes(self, saida: dict[str, Any], lembretes: dict[str, str]) -> dict[str, Any]:
        """Põe na saída os lembretes novos (texto) e só a chave dos que já foram dados nesta conversa."""
        novos = {k: v for k, v in lembretes.items() if k not in self.lembretes_dados}
        ja = [k for k in lembretes if k in self.lembretes_dados]
        self.lembretes_dados |= set(novos)
        if novos:
            saida["lembretes"] = novos
        if ja:
            saida["lembretes_ja_ditos"] = {"chaves": ja, "nota": NOTA_JA_DITO}
        return saida

    def _enviar(self, evento: tuple[str, dict[str, Any]]) -> None:
        if self.emitir is not None:
            self.emitir(evento)
        else:
            self.pendentes.append(evento)

    def ferramenta(self, nome: str, resumo: str) -> None:
        self._enviar(("ferramenta", {"nome": nome, "nivel": NIVEIS[nome], "resumo": resumo}))

    def proposta(self, proposta: dict[str, Any]) -> None:
        self._enviar(("proposta", proposta))

    def esvaziar(self) -> list[tuple[str, dict[str, Any]]]:
        saida, self.pendentes = self.pendentes, []
        return saida

    def nova_resposta(self, blocos: list[Any]) -> None:
        self.chamadas = [(b.name, b.id, b.input) for b in blocos if getattr(b, "type", None) == "tool_use"]

    def tomar_chamada(self, nome: str, entrada: Any) -> str | None:
        """O id do tool_use desta execução. O SDK passa só a entrada à ferramenta; casar por nome + entrada exata (e
        consumir) acerta mesmo com duas chamadas da mesma ferramenta na mesma resposta."""
        for i, (n, id_, e) in enumerate(self.chamadas):
            if n == nome and e == entrada:
                del self.chamadas[i]
                return id_
        return None

    def chave_idempotencia(self) -> str | None:
        # (sessão, tool_use): repetir a MESMA chamada nunca cria duas propostas; outra chamada é outra chave
        if self.sessao_id and self.chamada_atual:
            return f"{self.sessao_id}:{self.chamada_atual}"
        return None


def _json(dado: Any) -> str:
    return json.dumps(dado, ensure_ascii=False)


def _sem_acento(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t.casefold()) if unicodedata.category(c) != "Mn")


def _erro(e: ErroApi) -> ToolError:
    if isinstance(e, ApiForaDoAr):
        # A volta para logo depois desta rodada (conversa.py lê ApiCentral.fora_do_ar); o texto fica no histórico.
        return ToolError("O sistema está fora do ar: a consulta não foi feita. Não tente de novo agora.")
    if e.status in (401, 403):
        return ToolError(f"Sem permissão para isso (HTTP {e.status}): {e.mensagem}")
    if e.status == 404:
        return ToolError(f"Não encontrado: {e.mensagem}")
    return ToolError(f"A API recusou (HTTP {e.status}): {e.mensagem}")


def _qtd_un(m: int, unidade: str) -> str:
    return f"{fmt_qtd(m)} {unidade}".strip()


# Datas para gente: "16/11/2026", "ago/2027". O modelo repetia o ISO da API ("2027-08") na resposta.
def fmt_data(d: str | None) -> str | None:
    if not d:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", d.strip())
    return f"{m[3]}/{m[2]}/{m[1]}" if m else d


def fmt_mes(mes: str | None) -> str | None:
    if not mes:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})", mes.strip())
    return f"{_MESES[int(m[2]) - 1]}/{m[1]}" if m and 1 <= int(m[2]) <= 12 else mes


def _mes_iso(texto: str) -> str | None:
    """ "2026-12", "dez/2026", "dez/26", "12/2026" → "2026-12"; None se não entender."""
    t = _sem_acento(texto.strip())
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", t):
        a, n = int(m[1]), int(m[2])
    elif m := re.fullmatch(r"([a-z]{3})[a-z]*[/ ]+(?:de )?(\d{2}|\d{4})", t):
        if m[1] not in _MESES:
            return None
        a, n = int(m[2]), _MESES.index(m[1]) + 1
    elif m := re.fullmatch(r"(\d{1,2})/(\d{2}|\d{4})", t):
        a, n = int(m[2]), int(m[1])
    else:
        return None
    a = a + 2000 if a < 100 else a
    return f"{a:04d}-{n:02d}" if 1 <= n <= 12 else None


def _pct2(c: int | None) -> str | None:
    """Como a tela do cronograma: "88,86%", "100%", "12,5%"."""
    if c is None:
        return None
    t = f"{c / 100:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{t}%"


def _pct(c: int | None) -> str | None:
    return None if c is None else f"{c / 100:.1f}".replace(".", ",") + "%"


def _reais_ou_none(c: int | None) -> str | None:
    return None if c is None else fmt_reais(c)


def _meses(decimos: int | None) -> str | None:
    if decimos is None:
        return None
    if decimos == 0:
        return "em dia"
    n = abs(decimos) / 10
    t = (f"{n:.1f}".replace(".", ",") if decimos % 10 else f"{n:.0f}") + (" mês" if n <= 1 else " meses")
    return f"{t} de atraso" if decimos > 0 else f"adiantada {t}"


def _etapa_no_mes(x: dict[str, Any]) -> str:
    """ "3 FUNDAÇÕES: 70% no mês (R$ 74.119,60), continua (adiantada)". Começa ou continua vem da API (`comeca`, pela
    medição): o resumo dizia "em dezembro começam as Fundações" com 4,51% delas já medidos."""
    t = f"{x['etapa']} {x['descricao']}: {_pct2(x['pct_no_mes'])} no mês ({fmt_reais(x['valor_no_mes'])})"
    if x.get("comeca"):
        return t + ", começa neste mês"
    sit = _SITUACAO.get(x.get("situacao") or "")
    if not sit or x.get("situacao") == "nao_comecou":
        return t
    return t + f", continua ({sit})"


def _resumo(r: dict[str, Any], eventos: Eventos) -> dict[str, Any]:
    """Só formata o que a API somou (GET /obras/{id}/resumo). Bloco sem dado vira um lembrete (uma vez por conversa),
    que o modelo põe no fim da resposta como próximo passo."""
    o, c, f, pend = r["obra"], r["cronograma"], r["financeiro"], r["pendencias"]
    status = {"em_andamento": "em andamento", "em_orcamento": "em orçamento", "concluida": "concluída"}
    falta_cadastro: dict[str, str] = {}
    etapa = _etapa_no_mes

    if c["cadastrado"]:
        prazo: dict[str, Any] = {
            "termino_contratual": fmt_mes(c.get("termino_contratual")),
            "termino_projetado": fmt_mes(c.get("termino_projetado")),
            "situacao_contra_atual": _meses(c.get("atraso_decimos")),
            "situacao_contra_original": _meses(c.get("atraso_base_decimos")) if c.get("revisoes") else None,
            "ultima_medicao": fmt_mes(c.get("mes_ultima_medicao")),
            "medido_x_previsto": f"{_pct(c['pct_medido'])} medido × {_pct(c['pct_previsto_na_medicao'])} previsto"
            if c.get("pct_medido") is not None else None,
            "mes_corrente": fmt_mes(c["mes_corrente"]),
            "previsto_no_mes_corrente": _reais_ou_none(c.get("previsto_no_mes")),
            "pct_previsto_acumulado_no_fim_do_mes": _pct(c.get("pct_previsto_acumulado_no_mes")),
            "etapas_do_mes": [etapa(x) for x in c["etapas_do_mes"]],
            # o mês seguinte inteiro, maior valor primeiro (só as que começavam deixava de fora a 2ª maior frente)
            "proximo_mes": {"mes": fmt_mes(c["proximo_mes"]),
                            "previsto": _reais_ou_none(c.get("previsto_no_proximo_mes")),
                            "pct_previsto_acumulado_no_fim_do_mes":
                                _pct(c.get("pct_previsto_acumulado_no_proximo_mes")),
                            "etapas": [etapa(x) for x in c.get("etapas_do_proximo_mes") or []]}
            if c.get("proximo_mes") else None,
            # só quando é depois do mês seguinte: as que começam no seguinte já estão marcadas lá
            "proxima_a_comecar": {"mes": fmt_mes(c["proximo_mes_com_inicio"]),
                                  "etapas": [etapa(x) for x in c["proximas_etapas"]]}
            if c.get("proximo_mes_com_inicio") and c["proximo_mes_com_inicio"] != c.get("proximo_mes") else None,
            "etapas_atrasadas": [f"{x['etapa']} {x['descricao']}: {_pct2(x['medido_acumulado'])} medido de "
                                 f"{_pct2(x['previsto_ate_referencia'])} previsto" for x in c["etapas_atrasadas"]],
        }
        if not c.get("medicoes"):
            prazo["aviso"] = "Nenhuma medição lançada: ainda não há atraso para medir."
    else:
        prazo = {"cadastrado": False, "termino_contratual": fmt_mes(c.get("termino_contratual"))}
        falta_cadastro[f"sem_cronograma:{o['id']}"] = ("Cronograma não cadastrado: cadastre em Cronograma para "
                                                      "acompanhar prazo e etapas.")
    # O gasto é o dinheiro que saiu, por comprovante confirmado, e o saldo é o que falta gastar (negativo = passou do
    # orçado).
    if f["tem_orcamento"]:
        financeiro: dict[str, Any] = {
            "orcado_atual": fmt_reais(f["orcado_atual"]), "aditivos": fmt_reais(f["aditivos"]),
            "aditivos_pct": _pct(f.get("aditivos_pct")), "gasto": fmt_reais(f["gasto"]),
            "falta_gastar": fmt_reais(f["saldo"]),
            "etapas_estouradas": [f"{x['etapa']} {x['descricao']} (passou {fmt_reais(-x['saldo'])} do orçado)"
                                  for x in f["etapas_estouradas"]],
        }
    else:
        financeiro = {"tem_orcamento": False}
        if f.get("gasto"):
            financeiro["gasto"] = fmt_reais(f["gasto"])
        falta_cadastro[f"sem_orcamento:{o['id']}"] = ("Orçamento não importado: importe em Orçamento para acompanhar "
                                                     "custo por etapa.")
    if gastos := _gastos_por_comprovante(f.get("gastos")):
        financeiro["gastos_por_comprovante"] = gastos
    return eventos.com_lembretes({
        "obra": {"obra_id": o["id"], "nome": o["nome"], "cidade": o.get("cidade"),
                 "situacao": status.get(o.get("status"), o.get("status"))},
        "hoje": fmt_data(r["hoje"]),
        "prazo": prazo,
        "financeiro": financeiro,
        "aprovacoes_pendentes": {"quantas": pend["propostas"], "titulos": pend["titulos"]},
        "fonte": "resumo da obra (cronograma, orçamento, gastos e aprovações)",
    }, falta_cadastro)


_DATA_NO_TEXTO = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b")


def _datas_futuras(texto: str | None, hoje: date) -> list[str]:
    """Datas "30/11" ou "30/11/2026" escritas num texto livre (observação da medição) que ainda não chegaram."""
    saida = []
    for d, m, a in _DATA_NO_TEXTO.findall(texto or ""):
        try:
            quando = date(int(a) if a else hoje.year, int(m), int(d))
        except ValueError:
            continue
        if quando > hoje:
            saida.append(quando.strftime("%d/%m/%Y"))
    return saida


MAX_GASTOS_TRIBUTOS = 10


def fmt_cno(cno: str | None) -> str | None:
    """900123456789 → 90.012.34567/89 (a máscara do Cadastro Nacional de Obras)."""
    if not cno or len(cno) != 12:
        return cno or None
    return f"{cno[:2]}.{cno[2:5]}.{cno[5:10]}/{cno[10:]}"


def _gasto_tributo(g: dict[str, Any]) -> dict[str, Any]:
    return {"favorecido": g.get("favorecido"), "descricao": g.get("descricao"), "valor": fmt_reais(g["valor"]),
            "data": fmt_data(g.get("data_pagamento"))}


_FATOR_AJUSTE = {
    "aplicado": "aplicado: a remuneração declarada passa do mínimo e a DCTFWeb está em dia, nada a pagar na aferição",
    "atingido": "atingido: falta só confirmar a DCTFWeb entregue em todos os meses para o INSS zerar",
    "nao_atingido": "não atingido",
    "nao_se_aplica": "não se aplica (só obra de pessoa física)",
}


def _tributos(t: dict[str, Any]) -> dict[str, Any]:
    """Os dois tributos com as linhas da conta como a API montou (a conta é do domínio, aqui só se formata)."""
    i, b = t["inss"], t["ibs_cbs"]
    inss: dict[str, Any] = {"situacao": i["situacao"], "motivos": i["motivos"], "linhas_conta": i["linhas"]}
    if i["situacao"] == "calculado":
        inss |= {
            "valor_estimado_centavos": i["inss"], "valor_estimado": fmt_reais(i["inss"]),
            "mao_de_obra_presumida": fmt_reais(i["rmt"]), "comprovado_pelos_gastos": fmt_reais(i["comprovado"]),
            "base": fmt_reais(i["base"]), "fator_social": _pct2(i["fator_social"]) if i["fator_social"] < 10000
            else None,
            "mao_de_obra_paga_sem_nota": fmt_reais(i["mao_de_obra_sem_nf"]),
            "notas_sem_o_cno_da_obra": fmt_reais(i.get("notas_sem_cno", 0)),
            "economia_se_tiver_nota_centavos": i["economia_com_notas"],
            "economia_se_tiver_nota": fmt_reais(i["economia_com_notas"]),
            # fator de ajuste (só obra de pessoa física): com remuneração declarada suficiente e a DCTFWeb em dia,
            # o INSS da aferição vai a zero; "falta_declarar" é o caminho para chegar lá
            "fator_de_ajuste": _FATOR_AJUSTE.get(i.get("fator_ajuste", "")),
            "falta_declarar_para_o_fator_de_ajuste": fmt_reais(i["falta_fator_ajuste"])
            if i.get("fator_ajuste") == "nao_atingido" else None,
            "avisos": i.get("avisos", []),
        }
    ibs: dict[str, Any] = {"situacao": b["situacao"], "motivos": b["motivos"], "linhas_conta": b["linhas"],
                           "transicao": b["transicao"], "creditos_das_compras": fmt_reais(b["creditos"])}
    if b["situacao"] == "calculado":
        ibs |= {"a_pagar_centavos": b["a_pagar"], "a_pagar": fmt_reais(b["a_pagar"]), "debito": fmt_reais(b["debito"]),
                "base": fmt_reais(b["base"]), "aliquota_efetiva": _pct2(b["aliquota_efetiva"])}
    return {
        "obra_id": t["obra_id"],
        "fonte": "tributos estimados pelo sistema (INSS da aferição e IBS/CBS)",
        "cno": fmt_cno(t.get("cno")),
        "mes_do_vau": fmt_mes(t.get("vau_referencia")),
        "aferida_em": fmt_mes(t.get("afericao")),
        # de onde veio o m² da conta: a tabela oficial do SERO para a UF e a destinação, ou um valor à mão da obra
        "vau": fmt_reais(t["vau_usado"]) + "/m²" if t.get("vau_usado") else None,
        "origem_do_vau": {"tabela_sero": "tabela VAU oficial do SERO (Receita)", "informado": "informado na obra"}.get(
            t.get("vau_fonte") or ""),
        "inss": inss,
        "ibs_cbs": ibs,
        "gastos_a_classificar": {"quantos": t["a_classificar"], "valor": fmt_reais(t["valor_a_classificar"])},
        "mao_de_obra_sem_nota": [_gasto_tributo(g) for g in t["sem_nota"][:MAX_GASTOS_TRIBUTOS]],
        "pendentes_de_classificar": [_gasto_tributo(g) for g in t["pendentes"][:MAX_GASTOS_TRIBUTOS]],
        "aviso": t["aviso"],
    }


def _gastos_por_comprovante(g: dict[str, Any] | None) -> dict[str, str] | None:
    """Os gastos lançados por comprovante, como a API repartiu: já estão no gasto da obra. Só o que não for zero, para
    o modelo não comentar linha vazia."""
    if not g or not g.get("total"):
        return None
    rotulos = {"total": "total", "fora_do_orcamento": "fora_do_orcamento_etapas_extras",
               "sem_etapa_do_orcamento": "sem_etapa_do_orcamento_falta_reclassificar"}
    return {nome: fmt_reais(g[k]) for k, nome in rotulos.items() if g.get(k)}


def criar_ferramentas(api: ApiCentral, eventos: Eventos) -> list:
    async def _propor(corpo: dict[str, Any], extra: dict[str, Any] | None = None) -> str:
        chave = eventos.chave_idempotencia()
        if chave:
            corpo = {**corpo, "chave_idempotencia": chave}
        try:
            proposta = await api.criar_proposta(corpo)
        except ErroApi as e:
            raise _erro(e) from None
        eventos.proposta(proposta)
        if proposta.get("ja_existia"):
            status = proposta.get("status", "pendente")
            aviso = ("Já havia uma proposta igual aguardando aprovação; nada novo foi criado. Diga à pessoa que ela "
                     "já está esperando a aprovação." if status == "pendente"
                     else f"Esta proposta já tinha sido criada e agora está {status}; nada novo foi criado.")
        else:
            aviso = "Proposta criada e aguardando aprovação de uma pessoa. Nada foi executado ainda."
        return _json({
            "proposta_id": proposta.get("id"),
            "status": proposta.get("status", "pendente"),
            "titulo": proposta.get("titulo"),
            "ja_existia": bool(proposta.get("ja_existia")),
            "aviso": aviso,
            **(extra or {}),
        })

    def _com_chamada(ferramenta):
        """O SDK chama `ferramenta.call(entrada)` sem o id do tool_use; aqui ele é achado antes da chamada (mesmo que a
        entrada não valide e a função nem rode) para a proposta levar a chave de idempotência."""
        original = ferramenta.call

        async def call(entrada: object):
            eventos.chamada_atual = eventos.tomar_chamada(ferramenta.name, entrada)
            try:
                return await original(entrada)
            finally:
                eventos.chamada_atual = None

        ferramenta.call = call
        return ferramenta

    @beta_async_tool(eager_input_streaming=True)
    async def listar_obras() -> str:
        """Lista as obras que a pessoa pode ver, com id, nome, cidade, status, valor do contrato e prazo.

        Use para descobrir o obra_id quando a pessoa citar a obra pelo nome ou não houver obra em foco.
        Não use para números da obra: para isso há resumo_obra, consultar_orcamento e consultar_cronograma.
        """
        eventos.ferramenta("listar_obras", "consultou as obras")
        try:
            obras = await api.obras()
        except ErroApi as e:
            raise _erro(e) from None
        return _json([
            {
                "obra_id": o["id"],
                "nome": o.get("nome"),
                "cidade": o.get("cidade"),
                "status": o.get("status"),
                "numero_contrato": o.get("numero_contrato") or None,
                "valor_do_contrato_centavos": o.get("verba", 0),
                "valor_do_contrato": fmt_reais(o.get("verba", 0)),
                "inicio": fmt_data(o.get("inicio")),
                "prazo_meses": o.get("prazo_meses") or None,
            }
            for o in obras
        ])

    @beta_async_tool(eager_input_streaming=True)
    async def consultar_orcamento(obra_id: str, busca: str | None = None) -> str:
        """Mostra o orçamento da obra e o orçado × gasto por etapa, com as contas já feitas pelo sistema.

        Traz: valor do contrato, aditivos (total, % sobre o contrato, cada aditivo com nº, descrição e data), orçado
        atual, e por etapa: orçado, gasto (os comprovantes confirmados na etapa) e falta gastar (negativo = passou do
        orçado). Gastos em etapa extra ou ainda sem etapa do orçamento só entram nos totais. Use para "quanto custou o
        aditivo", "a fundação estourou?", "quanto já gastamos". Orçado é preço de contrato (com BDI e mão de obra); o
        gasto é custo, então o gasto não é o % executado (para isso, consultar_cronograma).

        Args:
            obra_id: ID da obra.
            busca: Parte da descrição de um serviço do orçamento (ex.: "tapume") para listar os itens que batem.
        """
        eventos.ferramenta("consultar_orcamento", f"consultou o orçamento{f' (busca: {busca})' if busca else ''}")
        try:
            o = await api.orcamento(obra_id)
        except ErroApi as e:
            raise _erro(e) from None
        t = o["totais"]
        contrato = next((v for v in o["versoes"] if v["ativa"] and v["tipo"] == "contrato"), None)
        aditivos = [v for v in o["versoes"] if v["ativa"] and v["tipo"] == "aditivo"]
        # Saldo, estouro e % do aditivo vêm prontos da API: aqui só se formata o que chegou.
        saida: dict[str, Any] = {
            "obra_id": obra_id,
            "fonte": "orçamento da obra (versões vigentes) e gastos",
            "tem_orcamento": contrato is not None,
            "contratado": fmt_reais(t["contratado"]),
            "aditivos": fmt_reais(t["aditivos"]),
            "aditivos_pct_sobre_contrato": _pct(t.get("aditivos_pct")),
            "orcado_atual": fmt_reais(t["atual"]),
            "gasto": fmt_reais(t["gasto"]),
            "falta_gastar": _reais_ou_none(t.get("saldo")),
            "lista_aditivos": [{"numero": v["numero"], "descricao": v.get("descricao"), "data": fmt_data(v.get("data")),
                                "valor": fmt_reais(v["total"]), "itens": v["itens"]} for v in aditivos],
            "etapas": [{
                "etapa": e["etapa"], "descricao": e["descricao"], "orcado_atual": fmt_reais(e["atual"]),
                "aditivos": fmt_reais(e["aditivos"]), "gasto": fmt_reais(e["gasto"]),
                "falta_gastar": _reais_ou_none(e.get("saldo")),
                "estourou": e.get("estourou"),
            } for e in o["etapas"]],
        }
        if gastos := _gastos_por_comprovante(t.get("gastos")):
            saida["gastos_por_comprovante"] = gastos
        if t["sem_etapa"]["gasto"]:
            saida["sem_etapa"] = {"gasto": fmt_reais(t["sem_etapa"]["gasto"])}
        achados: list[dict[str, Any]] = []
        if busca:
            alvo = _sem_acento(busca)
            achados = [x for x in o["itens"] if x["tipo"] == "item" and alvo in _sem_acento(x["descricao"])]
        # A data futura de um aditivo só vira lembrete numa busca que achou item dele. Na visão geral o orçamento serve
        # a "gasto por etapa" e "a fundação estourou?", e o lembrete fechava essas respostas também (rodadas 3 e 7 do
        # teste): ali a data fica marcada no próprio aditivo, para o modelo avisar só se falar dele.
        versoes_achadas = {x.get("versao_id") for x in achados}
        datas = {}
        for v, item in zip(aditivos, saida["lista_aditivos"], strict=True):
            if (v.get("data") or "") <= eventos.hoje.isoformat():
                continue
            if not busca:
                item["data_depois_de_hoje"] = "pode estar errada: avise só se a resposta falar deste aditivo"
            elif v["id"] in versoes_achadas:
                datas[f"data_futura:aditivo:{obra_id}:{v['numero']}"] = (
                    f"O aditivo {v['numero']} está com data {fmt_data(v['data'])}, depois de hoje: avise uma vez, "
                    "curto, que a data pode estar errada.")
        saida = eventos.com_lembretes(saida, datas)
        if busca:
            saida["itens_encontrados"] = [{
                "numero": x["numero"], "etapa": x["etapa"], "codigo": x.get("codigo"), "fonte": x.get("fonte"),
                "descricao": x["descricao"], "quantidade": _qtd_un(x["quantidade"], x["unidade"]),
                "valor_unitario": fmt_reais(x["valor_unitario"]), "total": fmt_reais(x["total"]),
                "versao": "aditivo" if any(v["id"] == x["versao_id"] for v in aditivos) else "contrato",
            } for x in achados[:30]]
            if len(achados) > 30:
                saida["itens_encontrados_total"] = len(achados)
        return _json(saida)

    @beta_async_tool(eager_input_streaming=True)
    async def consultar_cronograma(obra_id: str, mes: str | None = None) -> str:
        """Mostra o cronograma físico-financeiro, as medições e o prazo, com as contas feitas pelo sistema, POR ETAPA.

        Traz: término contratual e projetado, atraso contra o cronograma atual e contra o original (linha de base, que
        a reprogramação não apaga), % previsto × % medido na última medição, previsto e medido por mês, as medições e,
        por etapa: situação na última medição (atrasada, em dia, adiantada, não começou, concluída), % previsto até lá
        × % medido, e a grade de cada mês (% e R$ previstos, % medido). Use para "a obra está atrasada?", "tem etapa
        atrasada?", "previsto × medido por etapa", "o que vem em dezembro?" (mande mes) e "quanto falta medir?".

        Args:
            obra_id: ID da obra.
            mes: Um mês para detalhar o que está previsto nele, "2026-12" ou "dez/2026". Omita para a visão geral.
        """
        eventos.ferramenta("consultar_cronograma",
                           f"consultou o cronograma e as medições{f' ({mes})' if mes else ''}")
        try:
            c = await api.cronograma(obra_id)
        except ErroApi as e:
            raise _erro(e) from None
        p = c["prazo"]
        if not c.get("inicio"):
            return _json({"obra_id": obra_id, "tem_cronograma": False,
                          "termino_contratual": fmt_mes(p.get("termino_contratual")),
                          "aviso": "A obra ainda não tem cronograma no sistema: não há como projetar o prazo. "
                                   "Sugira cadastrar em Cronograma."})
        meses = [m["mes"] for m in c["mensal"]]
        etapas = []
        for e in c["etapas"]:
            ps = e.get("percentuais") or []
            if not any(ps) and not e.get("medido_acumulado"):
                continue
            valores, medidos = e.get("valores_mes") or [], e.get("medido_mes") or []
            grade = {}
            for k, mes_k in enumerate(meses):
                pct = ps[k] if k < len(ps) else 0
                med = medidos[k] if k < len(medidos) else 0
                if pct or med:
                    celula = {}
                    if pct:
                        celula["previsto"] = _pct2(pct) + (f" · {fmt_reais(valores[k])}" if k < len(valores) else "")
                    if med:
                        celula["medido"] = _pct2(med)
                    grade[fmt_mes(mes_k)] = celula
            linha = {"etapa": e["etapa"], "descricao": e["descricao"], "orcado": fmt_reais(e.get("atual", 0)),
                     "situacao": _SITUACAO.get(e.get("situacao") or "", None),
                     "previsto_ate_a_medicao": _pct2(e.get("previsto_ate_referencia", 0)),
                     "medido": _pct2(e.get("medido_acumulado", 0)), "meses": grade}
            if e.get("atraso_pct"):
                linha["abaixo_do_previsto"] = _pct2(e["atraso_pct"]).replace("%", " pontos percentuais")
            etapas.append(linha)
        saida: dict[str, Any] = {
            "obra_id": obra_id,
            "fonte": "cronograma físico-financeiro e medições da obra",
            "tem_cronograma": True,
            "inicio": fmt_mes(c["inicio"]), "meses": c["meses"],
            "termino_contratual": fmt_mes(p.get("termino_contratual")),
            "termino_projetado": fmt_mes(p.get("termino_projetado")),
            "situacao_contra_cronograma_atual": _meses(p.get("atraso_decimos")),
            "reprogramacoes": c.get("revisoes", 0),
            "cronograma_original": {"inicio": fmt_mes(c["base"]["inicio"]), "meses": c["base"]["meses"]}
            if c.get("base") else None,
            "termino_original": fmt_mes(p.get("termino_base")),
            "situacao_contra_original": _meses(p.get("atraso_base_decimos")),
            "mes_da_ultima_medicao": fmt_mes(p.get("mes_referencia")),
            "pct_previsto_na_medicao": _pct(p.get("pct_previsto_na_medicao")),
            "pct_medido": _pct(p.get("pct_medido")),
            "sem_medicao": not c["medicoes"],
            "etapas_atrasadas": [x["etapa"] for x in etapas if x["situacao"] == "atrasada"],
            "etapas": etapas,
            "mensal": [{"mes": fmt_mes(m["mes"]), "previsto": fmt_reais(m["previsto"]),
                        "pct_previsto_acumulado": _pct(m["pct_previsto_acumulado"]),
                        "medido": None if m["medido"] is None else fmt_reais(m["medido"]),
                        "pct_medido_acumulado": _pct(m["pct_medido_acumulado"])} for m in c["mensal"]],
            "medicoes": [{"numero": m["numero"], "mes": fmt_mes(m["mes"]), "valor": fmt_reais(m["valor"]),
                          "pct_obra_acumulado": _pct(m["pct_obra_acumulado"]), "observacao": m.get("observacao")}
                         for m in c["medicoes"]],
        }
        alvo = None
        if mes:
            alvo = _mes_iso(mes)
            if alvo is None:
                raise ToolError(f"Mês '{mes}' não entendido: use 2026-12 ou dez/2026.")
        datas = {}
        for m in c["medicoes"]:
            # Pergunta de um mês ("o que vem em dezembro?") não é sobre a medição de outro mês: a data estranha na
            # observação dela fica para quando as medições forem o assunto (visão geral ou o mês dela).
            if alvo and m.get("mes") != alvo:
                continue
            if (m.get("mes") or "") > eventos.hoje.isoformat()[:7]:
                datas[f"data_futura:medicao:{obra_id}:{m['numero']}"] = (
                    f"A medição {m['numero']} é de {fmt_mes(m['mes'])}, mês que ainda não chegou: avise uma vez que o "
                    "mês pode estar errado.")
            futuras = _datas_futuras(m.get("observacao"), eventos.hoje)
            if futuras:
                datas[f"data_futura:medicao_obs:{obra_id}:{m['numero']}"] = (
                    f"A observação da medição {m['numero']} cita {', '.join(futuras)}, depois de hoje: avise uma vez, "
                    "curto, que a data pode estar errada.")
        saida = eventos.com_lembretes(saida, datas)
        if alvo:
            if alvo not in meses:
                saida["no_mes"] = {"mes": fmt_mes(alvo), "aviso": "Esse mês está fora do cronograma."}
            else:
                k = meses.index(alvo)
                m = c["mensal"][k]
                no_mes = [{"etapa": e["etapa"], "descricao": e["descricao"],
                           "previsto_no_mes": _pct2(e["percentuais"][k]),
                           "valor_no_mes": fmt_reais(e["valores_mes"][k]) if k < len(e.get("valores_mes") or []) else
                           None, "situacao_hoje": _SITUACAO.get(e.get("situacao") or "", None)}
                          for e in c["etapas"] if k < len(e.get("percentuais") or []) and e["percentuais"][k]]
                saida["no_mes"] = {"mes": fmt_mes(alvo), "previsto_no_mes": fmt_reais(m["previsto"]),
                                   "pct_previsto_acumulado_no_fim_do_mes": _pct(m["pct_previsto_acumulado"]),
                                   "etapas": no_mes}
        return _json(saida)

    @beta_async_tool(eager_input_streaming=True)
    async def resumo_obra(obra_id: str) -> str:
        """O panorama da obra numa consulta só, com tudo calculado pelo sistema. USE PRIMEIRO em pergunta aberta
        ("como tá a obra?", "e aí?", "resumo", "o que tem pra hoje?"); as outras ferramentas só se a pessoa pedir
        detalhe.

        Traz: situação da obra; prazo (término, atraso contra o atual e o original, % medido × previsto); as etapas
        do mês corrente e as do mês seguinte (maior valor primeiro; cada uma diz se começa ou continua, pela
        medição), a próxima a começar depois disso e as atrasadas; financeiro (orçado atual, gasto, falta gastar,
        aditivos, etapas que passaram do orçado); propostas aguardando aprovação. O que não está cadastrado vem em
        lembretes.

        Args:
            obra_id: ID da obra.
        """
        eventos.ferramenta("resumo_obra", "consultou o resumo da obra")
        try:
            r = await api.resumo_obra(obra_id)
        except ErroApi as e:
            raise _erro(e) from None
        return _json(_resumo(r, eventos))

    @beta_async_tool(eager_input_streaming=True)
    async def calcular(expressao: str, unidade_resultado: str | None = None) -> str:
        """A calculadora. Toda conta que não veio pronta de outra ferramenta passa por aqui; nunca faça conta de
        cabeça. Devolve a conta formatada para mostrar à pessoa como veio ("7 m³ × R$ 130,00/m³ = R$ 910,00").

        Os números têm de vir de uma ferramenta ou da pessoa. Use para: valor total a partir do preço unitário, quanto
        falta medir (total − medido), soma de gastos. Não use para refazer uma conta que a ferramenta já trouxe pronta
        (falta gastar, atraso).

        Args:
            expressao: Só números, + − × ÷ (ou * /) e parênteses. Números com vírgula decimal e sem separador de
                milhar ("130,00", "2094833,38"). Depois de cada número, opcional, a unidade entre colchetes:
                "7 [m³] × 130,00 [R$/m³]", "2094833,38 [R$] − 82872,85 [R$]".
            unidade_resultado: Unidade do resultado: "R$" (arredonda ao centavo) ou a do item ("m³", "sc").
        """
        eventos.ferramenta("calcular", "fez uma conta")
        try:
            r = _calcular(expressao, unidade_resultado)
        except ErroConta as e:
            raise ToolError(f"Conta não feita: {e}") from None
        return _json({"conta": r.conta, "resultado": r.resultado,
                      "aviso": "Mostre a conta como veio, numa linha só dela."})

    @beta_async_tool(eager_input_streaming=True)
    async def consultar_tributos(obra_id: str) -> str:
        """Os tributos da obra estimados pelo sistema: o INSS da aferição (CNO/SERO) e o IBS/CBS da reforma tributária.

        Traz, para cada um, a situação (calculado, isenta, não incide, sem dados), os motivos, as linhas_conta prontas
        (área para cálculo, custo, mão de obra presumida, comprovado corrigido pela Selic, base, valor), e no INSS
        quanto cairia se a mão de obra sem nota (ou com nota sem o CNO da obra) fosse comprovada, o fator de ajuste
        (quanto falta declarar para o INSS zerar) e os avisos; no IBS/CBS, débito, créditos das compras e a fase da
        transição. Também lista os pagamentos de mão de obra sem comprovação e os gastos ainda sem classificação.
        Use para "quanto vou pagar de INSS?", "CNO", "aferição", "SERO", "fator de ajuste", "IBS/CBS", "imposto da
        obra", "como pago menos imposto?". Cite as linhas_conta como vieram; é estimativa.

        Args:
            obra_id: ID da obra.
        """
        eventos.ferramenta("consultar_tributos", "consultou os tributos da obra")
        try:
            t = await api.tributos(obra_id)
        except ErroApi as e:
            raise _erro(e) from None
        return _json(_tributos(t))

    @beta_async_tool(eager_input_streaming=True)
    async def propor_cronograma(obra_id: str, inicio: str, etapas: list[EtapaDoCronograma], justificativa: str) -> str:
        """Propõe o cronograma físico-financeiro da obra por VISÃO DE OBRA. NÃO grava: cria uma proposta que uma pessoa
        aprova na tela.

        Use quando a pessoa pedir para montar/gerar o cronograma e a obra já tiver orçamento (veja as etapas por
        consultar_orcamento). VOCÊ, como mestre de obras, decide só a EQUIPE de cada etapa e a SEQUÊNCIA (o que depende
        do quê, o que corre em paralelo). A DURAÇÃO de cada etapa NÃO é sua: o sistema calcula pela quantidade de cada
        serviço × produtividade (homem-hora) ÷ equipe, e já respeita a cura do concreto entre as fases. Inclua todas as
        etapas do orçamento. SEMPRE proponha com as etapas que existirem — mesmo que seja uma só; não peça para a pessoa
        cadastrar mais etapas antes de propor (ela ajusta o orçamento e regenera depois, se quiser). Aprovada, a
        proposta grava o cronograma, e quem aprova pode desfazer.

        Args:
            obra_id: ID da obra.
            inicio: Mês de início da obra, "AAAA-MM" ou "mm/aaaa".
            etapas: As etapas do orçamento, cada uma com: etapa (a chave, ex.: "1", de consultar_orcamento), equipe
                (quantas pessoas na frente dessa etapa) e depende_de (lista das chaves de etapas que precisam terminar
                antes — deixe vazio para começar junto do início; a cura entra sozinha). Inclua todas as etapas.
            justificativa: Uma frase com o critério (ex.: "fundação → estrutura (após cura) → alvenaria; acabamento em
                paralelo onde dá").
        """
        eventos.ferramenta("propor_cronograma", "preparou uma proposta de cronograma")
        if not etapas:
            raise ToolError("Diga as etapas com a equipe e as dependências (a sequência da obra).")
        inicio_iso = _mes_iso(inicio)
        if not inicio_iso:
            raise ToolError("Início inválido: use AAAA-MM (ex.: 2026-01).")
        try:
            orc = await api.orcamento(obra_id)
        except ErroApi as e:
            raise _erro(e) from None
        # serviços por etapa, do orçamento (a conta é sobre a quantidade real; o modelo nunca manda quantidade)
        itens_por_etapa: dict[str, list[ServicoDoPlano]] = {}
        for x in orc.get("itens", []):
            if x.get("tipo") != "item" or not x.get("etapa"):
                continue
            chave = str(x["etapa"])
            itens_por_etapa.setdefault(chave, []).append(ServicoDoPlano(
                id=f"{chave}:{len(itens_por_etapa.get(chave, []))}", descricao=x.get("descricao", ""),
                quantidade_milesimos=int(x.get("quantidade") or 0), unidade=x.get("unidade", ""),
                codigo=str(x.get("codigo") or "")))
        # na ordem de obra: a chave da etapa é numérica ("1 Fundação", "2 Estrutura"...), então ordena por número
        ordem_orcamento = sorted((str(e["etapa"]) for e in orc.get("etapas", []) if str(e["etapa"]) in itens_por_etapa),
                                 key=lambda k: int(k) if k.isdigit() else 999)
        if not ordem_orcamento:
            raise ToolError("O orçamento não tem serviços com etapa; não dá para montar o cronograma.")
        decisao = {str(e["etapa"]): e for e in etapas}
        planejadas: list[EtapaParaPlano] = []
        anterior: str | None = None
        for chave in ordem_orcamento:  # toda etapa do orçamento entra, na ordem dele
            d = decisao.get(chave)
            equipe = int(d["equipe"]) if d and d.get("equipe") else 4
            if d and "depende_de" in d:
                dep = [str(x) for x in (d.get("depende_de") or []) if str(x) in itens_por_etapa]
            else:  # sem decisão: encadeia na etapa anterior (sequência natural de obra)
                dep = [anterior] if anterior else []
            planejadas.append(EtapaParaPlano(id=chave, itens=itens_por_etapa[chave], equipe=max(1, equipe),
                                             depende_de=dep))
            anterior = chave
        inicio_data = date(int(inicio_iso[:4]), int(inicio_iso[5:7]), 1)
        cr = montar_cronograma_de_etapas(planejadas, inicio_data)
        if cr.meses > 60:
            raise ToolError("O cronograma passa de 60 meses; reforce as equipes ou revise as dependências.")
        por_chave = {e.id: e for e in cr.etapas}
        dados_etapas = [{"etapa": chave, "percentuais": por_chave[chave].percentuais} for chave in ordem_orcamento]
        n = len(dados_etapas)
        semanas = round(cr.dias_uteis / 5)
        corpo = {
            "tipo": "cronograma",
            "obra_id": obra_id,
            "titulo": "Cronograma da obra",
            "conta": f"{n} {'etapa' if n == 1 else 'etapas'} · {cr.meses} {'mes' if cr.meses == 1 else 'meses'} "
                     f"· {semanas} {'semana' if semanas == 1 else 'semanas'} de obra",
            "justificativa": justificativa,
            "fonte": "orçamento da obra (quantidades × produtividade, com dependências e cura)",
            "dados": {"inicio": inicio_iso, "meses": cr.meses, "etapas": dados_etapas},
        }
        return await _propor(corpo)

    return [
        listar_obras,
        resumo_obra,
        consultar_orcamento,
        consultar_cronograma,
        consultar_tributos,
        calcular,
        _com_chamada(propor_cronograma),
    ]
