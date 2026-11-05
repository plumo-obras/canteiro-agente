"""As ferramentas do contrato. Cada uma chama a API central com o token do usuário.

Devolvem números brutos (milésimos, centavos, como a API manda) e formatados ("2.760 un", "R$ 472,00"), mais a
linha de conta pronta, para o modelo citar sem fazer conta. As de nível `propoe` só criam proposta (`POST /propostas`):
nada é executado sem a aprovação de uma pessoa.

As ferramentas são criadas por requisição (fecham sobre o token daquela conversa), mas nome, descrição e esquema são
sempre os mesmos bytes: é isso que mantém o cache do prefixo (ferramentas + instrução) válido entre conversas.
"""
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, TypedDict
from zoneinfo import ZoneInfo

from anthropic import beta_async_tool
from anthropic.lib.tools import ToolError

from canteiro_dominio.modelos import StatusPedido
from canteiro_dominio.regras import RegraViolada, validar_transicao
from canteiro_dominio.tipos import fmt_qtd, fmt_reais, qtd, reais

from .api_central import ApiCentral, ApiForaDoAr, ErroApi
from .calculo import ErroConta
from .calculo import calcular as _calcular

Nivel = Literal["consulta", "propoe"]
NIVEIS: dict[str, Nivel] = {
    "listar_obras": "consulta",
    "resumo_obra": "consulta",
    "consultar_estoque": "consulta",
    "historico_movimentos": "consulta",
    "listar_pedidos": "consulta",
    "consultar_orcamento": "consulta",
    "consultar_cronograma": "consulta",
    "listar_fornecedores": "consulta",
    # não fala com a API: é a calculadora. Fica como consulta porque não escreve nada.
    "calcular": "consulta",
    "propor_pedido": "propoe",
    "propor_entrega": "propoe",
    "propor_avanco_pedido": "propoe",
    "propor_ajuste_estoque": "propoe",
}
# Ferramentas que não chamam a API (o teste de "só propõe" confere que elas não fazem requisição nenhuma).
LOCAIS = {"calcular"}

StatusTexto = Literal["em_cotacao", "aprovado", "enviado", "em_transito", "recebido", "parcial", "conferido",
                      "cancelado"]
# Status em que o pedido ainda espera material (recebe entrega) e o que conta como "aberto" para a busca.
RECEBE_ENTREGA = ["aprovado", "enviado", "em_transito", "recebido", "parcial"]
ABERTOS = ["em_cotacao", *RECEBE_ENTREGA]
FUSO = ZoneInfo("America/Sao_Paulo")
_MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
_SITUACAO = {"concluida": "concluída", "atrasada": "atrasada", "adiantada": "adiantada", "em_dia": "em dia",
             "nao_comecou": "não começou"}


class LinhaEntregue(TypedDict):
    """Uma linha do pedido nesta entrega."""
    indice: int
    quantidade: str


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


def fmt_quando(iso: str | None) -> str | None:
    """Instante da API (UTC) no fuso de Brasília: "03/11/2026 16:45"."""
    if not iso:
        return None
    try:
        q = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if q.tzinfo:
        q = q.astimezone(FUSO)
    return q.strftime("%d/%m/%Y %H:%M")


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


def _data_iso(texto: str | None) -> str | None:
    """ "05/11/2026" ou "2026-11-05" → "2026-11-05" (o formato da API)."""
    if not texto:
        return None
    t = texto.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        return t
    if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", t):
        return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
    raise ToolError(f"Data '{texto}' não entendida: use dd/mm/aaaa.")


def linha_de_conta(conta: dict[str, Any], unidade: str) -> str:
    """A linha que a API montou (regras.texto_da_conta: sem previsão, falta ou sobra, sempre conta verdadeira). Sem ela
    (API antiga), só formata, sem nenhuma operação: nunca escreve "precisa 0 … = falta 0"."""
    if conta.get("texto"):
        return conta["texto"]
    f = lambda k: _qtd_un(conta.get(k, 0), unidade)  # noqa: E731
    tem = f"saldo {f('saldo')} · a caminho {f('a_caminho')} · em cotação {f('cotando')} · transferência " \
          f"{f('transferencia')}"
    if not conta.get("precisa"):
        return f"sem previsão lançada para a obra (o sistema não sabe quanto ela precisa) · {tem}"
    if not conta.get("falta"):
        return f"precisa {f('precisa')}; {tem}: cobre o que precisa, falta 0"
    return (
        f"precisa {f('precisa')} − saldo {f('saldo')} − a caminho {f('a_caminho')} − em cotação {f('cotando')}"
        f" − transferência {f('transferencia')} = falta {f('falta')}"
    )


AVISO_SEM_PREVISAO = ("Sem previsão lançada: o sistema não sabe quanto a obra precisa deste insumo. Isso NÃO quer "
                      "dizer que não precisa; diga que a previsão não foi lançada.")


def _item_estoque(item: dict[str, Any]) -> dict[str, Any]:
    insumo, conta = item["insumo"], item["conta"]
    un = insumo.get("unidade", "")
    saida = {
        "insumo_id": insumo["id"],
        "nome": insumo["nome"],
        "unidade": un,
        "curva": insumo.get("curva"),
        "conta_milesimos": {k: v for k, v in conta.items() if k != "texto"},
        "conta_formatada": {k: _qtd_un(v, un) for k, v in conta.items() if isinstance(v, int)
                            and not isinstance(v, bool)},
        "linha_conta": linha_de_conta(conta, un),
        "sem_previsao": bool(conta.get("sem_previsao", not conta.get("precisa"))),
    }
    if saida["sem_previsao"]:
        saida["aviso"] = AVISO_SEM_PREVISAO
    if conta.get("sobra"):
        saida["sobra"] = _qtd_un(conta["sobra"], un)
    preco = item.get("ultimo_preco")
    if preco:
        saida["ultimo_preco"] = {
            "unitario": f"{fmt_reais(preco['valor_unitario'])}/{un}" if un else fmt_reais(preco["valor_unitario"]),
            "unitario_centavos": preco["valor_unitario"],
            "pedido": preco.get("numero"), "fornecedor": preco.get("fornecedor"),
            "data": fmt_data(preco.get("data")),
        }
    else:
        saida["ultimo_preco"] = None
    if insumo.get("valor_unitario"):
        saida["preco_do_orcamento"] = f"{fmt_reais(insumo['valor_unitario'])}/{un}" if un else \
            fmt_reais(insumo["valor_unitario"])
    return saida


def _linhas(p: dict[str, Any]) -> list[dict[str, Any]]:
    # pedido de antes das linhas (um insumo só) ainda pode aparecer em dado antigo
    if p.get("linhas"):
        return p["linhas"]
    return [{"insumo_id": p.get("insumo_id"), "quantidade": p.get("quantidade", 0), "total": p.get("valor", 0),
             "recebida": p.get("quantidade_recebida") or 0, "nome": "", "unidade": ""}]


def _pedido(p: dict[str, Any]) -> dict[str, Any]:
    linhas = []
    for i, x in enumerate(_linhas(p)):
        un = x.get("unidade", "")
        linha = {
            "indice": i,
            "insumo_id": x.get("insumo_id"),
            "nome": x.get("nome"),
            "tipo": x.get("tipo", "material"),
            "etapa": x.get("etapa"),
            "quantidade": _qtd_un(x.get("quantidade", 0), un),
            "recebida": _qtd_un(x.get("recebida", 0), un),
            "total": fmt_reais(x.get("total", 0)),
        }
        # A conta é da API (regras.falta_na_linha; 0 em pedido encerrado). Sem ela, o modelo fica sem o número em vez
        # de ganhar uma subtração feita aqui.
        falta = x.get("falta_entregar")
        if falta is not None:
            linha |= {"falta_entregar": _qtd_un(falta, un), "falta_entregar_milesimos": falta}
        linhas.append(linha)
    return {
        "pedido_id": p["id"],
        "numero": p.get("numero"),
        "status": p.get("status"),
        "fornecedor": p.get("fornecedor"),
        "valor_centavos": p.get("valor"),
        "valor": fmt_reais(p.get("valor", 0)),
        "previsao": fmt_data(p.get("previsao")),
        "feito_em": fmt_quando(p.get("criado_em")),
        "entregas": p.get("recebimentos", 0),
        "aberto": p.get("status") in ABERTOS,
        "recebe_entrega": p.get("status") in RECEBE_ENTREGA,
        "linhas": linhas,
    }


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


def _entregas(xs: list[dict[str, Any]]) -> list[str]:
    return [f"{x['numero']} ({x['fornecedor']}): " + "; ".join(
        f"{_qtd_un(i['falta_entregar'], i['unidade'])} de {i['nome']}" for i in x["itens"])
        + (f", previsto para {fmt_data(x['previsao'])}" if x.get("previsao") else "") for x in xs]


def _resumo(r: dict[str, Any]) -> dict[str, Any]:
    """Só formata o que a API somou (GET /obras/{id}/resumo). Bloco sem dado vira um aviso, que o modelo põe no fim
    da resposta como próximo passo."""
    o, c, f, e, a, pend = (r["obra"], r["cronograma"], r["financeiro"], r["entregas"], r["almoxarifado"],
                           r["pendencias"])
    status = {"em_andamento": "em andamento", "em_orcamento": "em orçamento", "concluida": "concluída"}
    falta_cadastro = []

    def etapa(x: dict[str, Any]) -> str:
        t = f"{x['etapa']} {x['descricao']}: {_pct2(x['pct_no_mes'])} no mês ({fmt_reais(x['valor_no_mes'])})"
        sit = _SITUACAO.get(x.get("situacao") or "")
        return t + (f", {sit}" if sit and x.get("situacao") != "nao_comecou" else "")

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
            "proximas": {"mes": fmt_mes(c["proximo_mes_com_inicio"]), "etapas": [etapa(x) for x in
                                                                                c["proximas_etapas"]]}
            if c.get("proximo_mes_com_inicio") else None,
            "etapas_atrasadas": [f"{x['etapa']} {x['descricao']}: {_pct2(x['medido_acumulado'])} medido de "
                                 f"{_pct2(x['previsto_ate_referencia'])} previsto" for x in c["etapas_atrasadas"]],
        }
        if not c.get("medicoes"):
            prazo["aviso"] = "Nenhuma medição lançada: ainda não há atraso para medir."
    else:
        prazo = {"cadastrado": False, "termino_contratual": fmt_mes(c.get("termino_contratual"))}
        falta_cadastro.append("Cronograma não cadastrado: cadastre em Cronograma para acompanhar prazo e etapas.")
    if f["tem_orcamento"]:
        financeiro: dict[str, Any] = {
            "orcado_atual": fmt_reais(f["orcado_atual"]), "aditivos": fmt_reais(f["aditivos"]),
            "aditivos_pct": _pct(f.get("aditivos_pct")), "comprometido": fmt_reais(f["comprometido"]),
            "realizado_compras": fmt_reais(f["realizado"]), "saldo_a_comprometer": fmt_reais(f["saldo"]),
            "etapas_estouradas": [f"{x['etapa']} {x['descricao']} (saldo {fmt_reais(x['saldo'])})"
                                  for x in f["etapas_estouradas"]],
        }
    else:
        financeiro = {"tem_orcamento": False}
        if f.get("comprometido"):
            financeiro["comprometido"] = fmt_reais(f["comprometido"])
        falta_cadastro.append("Orçamento não importado: importe em Orçamento para acompanhar custo por etapa.")
    almox: dict[str, Any] = {
        "falta_comprar": [f"{x['nome']}: {x['texto']}" for x in a["falta_comprar"]],
        "consumo_por_semana": "o sistema ainda não projeta consumo por semana; a falta é até o fim da obra",
    }
    if a.get("sem_previsao"):
        almox["insumos_sem_previsao"] = a["sem_previsao"]
        falta_cadastro.append(f"{a['sem_previsao']} insumo(s) sem previsão de quantidade: sem ela, a falta deles não "
                              "é calculada (o que não quer dizer que não precisa).")
    return {
        "obra": {"obra_id": o["id"], "nome": o["nome"], "cidade": o.get("cidade"),
                 "situacao": status.get(o.get("status"), o.get("status"))},
        "hoje": fmt_data(r["hoje"]),
        "prazo": prazo,
        "financeiro": financeiro,
        "aprovacoes_pendentes": {"quantas": pend["propostas"], "titulos": pend["titulos"]},
        "entregas": {"hoje": _entregas(e["hoje"]), "atrasadas": _entregas(e["atrasadas"]),
                     "proximos_7_dias": _entregas(e["proximos_7_dias"]),
                     "sem_data_prevista": e["sem_previsao"], "nf_para_conferir": e["nf_para_conferir"]},
        "almoxarifado": almox,
        "falta_cadastrar": falta_cadastro,
        "fonte": "resumo da obra (cronograma, orçamento, pedidos, almoxarifado e aprovações)",
    }


def _ler_qtd(texto: str, campo: str) -> int:
    try:
        return qtd(texto)
    except (ValueError, ArithmeticError) as e:
        raise ToolError(f"{campo} inválido(a): {e}") from None


def _ler_reais(texto: str) -> int:
    try:
        return reais(texto.replace("R$", "").strip())
    except (ValueError, ArithmeticError) as e:
        raise ToolError(f"valor inválido: {e}") from None


def criar_ferramentas(api: ApiCentral, eventos: Eventos) -> list:
    async def _item_do_insumo(obra_id: str, insumo_id: str) -> dict[str, Any]:
        try:
            itens = await api.insumos(obra_id)
        except ErroApi as e:
            raise _erro(e) from None
        for item in itens:
            if item["insumo"]["id"] == insumo_id:
                return item
        raise ToolError("Esse insumo não existe nesta obra. Use consultar_estoque para achar o insumo_id certo.")

    async def _propor(corpo: dict[str, Any]) -> str:
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
        Não use para estoque ou pedidos: para isso há consultar_estoque e listar_pedidos.
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
    async def consultar_estoque(obra_id: str, busca: str | None = None) -> str:
        """Mostra os insumos do almoxarifado de uma obra com a conta de falta já calculada pelo sistema.

        Cada insumo traz precisa, saldo, a caminho, em cotação, transferência, falta e comprar (brutos em milésimos e
        formatados) e a linha_conta pronta para citar, além do último preço de compra (pedido, fornecedor, data) e do
        preço do orçamento. Use para "quanto tem", "quanto falta", "precisa comprar?", "quanto custa mais 10?". Não
        calcule a falta: cite a linha_conta. sem_previsao = ninguém lançou quanto a obra precisa (não quer dizer que
        não precisa). Custo de uma quantidade: calcular com o último preço.

        Args:
            obra_id: ID da obra (de listar_obras ou da obra em foco).
            busca: Parte do nome do insumo para filtrar (ex.: "aço", "cimento"). Omita para listar todos.
        """
        eventos.ferramenta("consultar_estoque", f"consultou o almoxarifado{f' (busca: {busca})' if busca else ''}")
        try:
            itens = await api.insumos(obra_id)
        except ErroApi as e:
            raise _erro(e) from None
        if busca:
            alvo = _sem_acento(busca)
            itens = [i for i in itens if alvo in _sem_acento(i["insumo"]["nome"])]
        return _json({"obra_id": obra_id, "fonte": "almoxarifado da obra",
                      "insumos": [_item_estoque(i) for i in itens]})

    @beta_async_tool(eager_input_streaming=True)
    async def historico_movimentos(obra_id: str, insumo_id: str, limite: int = 20) -> str:
        """Lista as últimas entradas, saídas e ajustes de um insumo (mais novos primeiro).

        Use para explicar de onde veio um saldo, quem deu baixa, para onde foi o material ou qual NF/pedido entrou.
        Não use para saber o saldo atual: consultar_estoque já traz a conta.

        Args:
            obra_id: ID da obra.
            insumo_id: ID do insumo (de consultar_estoque).
            limite: Quantos movimentos trazer, de 1 a 50.
        """
        eventos.ferramenta("historico_movimentos", "consultou o histórico de movimentos")
        try:
            movs = await api.movimentos(obra_id, insumo_id, max(1, min(limite, 50)))
        except ErroApi as e:
            raise _erro(e) from None
        return _json([
            {
                "tipo": m.get("tipo"),
                "quantidade_milesimos": m.get("quantidade"),
                "quantidade": fmt_qtd(m.get("quantidade", 0)),
                "destino": m.get("destino"),
                "etapa": m.get("etapa"),
                "origem_doc": m.get("origem_doc"),
                "quem": m.get("quem"),
                "origem": m.get("origem"),
                "quando": fmt_quando(m.get("criado_em")),
            }
            for m in movs
        ])

    @beta_async_tool(eager_input_streaming=True)
    async def listar_pedidos(obra_id: str, status: StatusTexto | None = None, abertos: bool = False,
                             busca: str | None = None) -> str:
        """Lista os pedidos de compra de uma obra: número (PED-NNNN), status, fornecedor, previsão, valor e as linhas
        (índice, insumo, etapa do orçamento, quantidade pedida, recebida e o que falta entregar).

        Use para "o que está a caminho", "o pedido do aço já chegou?", "falta chegar brita?", e SEMPRE antes de propor
        uma entrega ou mudança de status, para achar o pedido_id. Quando chega material ("chegou a brita da pedreira",
        "dá entrada da NF"), procure com abertos=true e busca = o fornecedor ou o insumo: o pedido certo é o que tem
        recebe_entrega=true e falta_entregar. Status "parcial" = chegou parte, o resto continua a caminho e ainda recebe
        entrega. Uma proposta ainda pendente não muda pedido nenhum: o pedido continua como está aqui.

        Args:
            obra_id: ID da obra.
            status: Filtra por um status. Omita para todos.
            abertos: true = só os que ainda não fecharam (em cotação até parcial).
            busca: Parte do nome do fornecedor ou de um insumo do pedido (ex.: "pedreira", "brita").
        """
        filtro = ABERTOS if abertos and not status else status
        rotulo = status or ("abertos" if abertos else "")
        rotulo += f"{', ' if rotulo else ''}busca: {busca}" if busca else ""
        eventos.ferramenta("listar_pedidos", f"consultou os pedidos{f' ({rotulo})' if rotulo else ''}")
        try:
            pedidos = await api.pedidos(obra_id, filtro)
        except ErroApi as e:
            raise _erro(e) from None
        if busca:
            alvo = _sem_acento(busca)
            pedidos = [p for p in pedidos if alvo in _sem_acento(p.get("fornecedor") or "")
                       or any(alvo in _sem_acento(x.get("nome") or "") for x in _linhas(p))]
        saida = [_pedido(p) for p in pedidos]
        if not saida and (abertos or busca or status):
            return _json({"pedidos": [], "aviso": "Nenhum pedido com esse filtro. Tente sem a busca antes de dizer "
                                                  "que o pedido não existe."})
        return _json(saida)

    @beta_async_tool(eager_input_streaming=True)
    async def propor_pedido(
        obra_id: str, insumo_id: str, quantidade: str, fornecedor: str, valor: str, justificativa: str,
        etapa: str | None = None, previsao: str | None = None,
    ) -> str:
        """Propõe um pedido de compra. NÃO compra: cria uma proposta que uma pessoa aprova ou recusa na tela.

        Use quando a pessoa pedir para comprar/pedir material e você já tiver insumo, quantidade, fornecedor e valor.
        Se faltar algum desses dados, pergunte antes; preço por unidade vira valor total com calcular. A justificativa
        descreve a situação de AGORA: proposta pendente não aconteceu ("se o encerramento do PED-0170 for aprovado",
        nunca "PED-0170, encerrado"). Aprovada, a proposta vira um pedido já aprovado (a caminho), sem
        segunda aprovação; quem aprova pode corrigir quantidade, fornecedor, valor e previsão antes. A conta de falta
        anexada vem do sistema, não de você.

        Args:
            obra_id: ID da obra.
            insumo_id: ID do insumo (de consultar_estoque).
            quantidade: Quantidade na unidade do insumo, sem separador de milhar e com vírgula decimal: "3200", "1,5".
            fornecedor: Nome do fornecedor, como a pessoa disse.
            valor: Valor total do pedido em reais, como a pessoa disse: "4720", "4.720,00", "1299,90".
            justificativa: Uma frase com o motivo (ex.: "repor a falta de aço para a laje do 2º pavimento").
            etapa: Etapa do orçamento que paga a compra (a chave, ex.: "3"; de consultar_orcamento). Se a obra tem
                orçamento e a pessoa não disse a etapa, pergunte. Omita só se a obra não tiver orçamento.
            previsao: Data prevista de entrega, dd/mm/aaaa, se a pessoa disse.
        """
        eventos.ferramenta("propor_pedido", "preparou uma proposta de pedido")
        q, v = _ler_qtd(quantidade, "quantidade"), _ler_reais(valor)
        if q <= 0 or v <= 0:
            raise ToolError("Quantidade e valor têm de ser maiores que zero.")
        item = await _item_do_insumo(obra_id, insumo_id)
        un = item["insumo"].get("unidade", "")
        try:
            etapas = {e["etapa"]: e["descricao"] for e in await api.etapas(obra_id)}
        except ErroApi as e:
            raise _erro(e) from None
        etapa = (etapa or "").strip() or None
        if etapa and etapa not in etapas:
            raise ToolError(f"A etapa {etapa} não existe no orçamento. Etapas: "
                            + "; ".join(f"{k} {v}" for k, v in etapas.items()))
        if etapas and not etapa:
            raise ToolError("Esta obra tem orçamento: diga de qual etapa é a compra (pergunte à pessoa se não souber). "
                            "Etapas: " + "; ".join(f"{k} {v}" for k, v in etapas.items()))
        dados: dict[str, Any] = {"insumo_id": insumo_id, "quantidade": q, "fornecedor": fornecedor, "valor": v}
        if etapa:
            dados["etapa"] = etapa
        if previsao:
            dados["previsao"] = _data_iso(previsao)
        return await _propor({
            "obra_id": obra_id,
            "tipo": "criar_pedido",
            "titulo": f"Comprar {_qtd_un(q, un)} de {item['insumo']['nome']} de {fornecedor} por {fmt_reais(v)}"
                      + (f" · etapa {etapa} {etapas[etapa]}" if etapa else ""),
            "justificativa": justificativa,
            "conta": linha_de_conta(item["conta"], un),
            "fonte": "almoxarifado da obra (conta calculada pelo sistema)",
            "dados": dados,
        })

    async def _pedido_da_obra(obra_id: str, pedido_id: str) -> dict[str, Any]:
        try:
            pedidos = await api.pedidos(obra_id)
        except ErroApi as e:
            raise _erro(e) from None
        pedido = next((p for p in pedidos if p["id"] == pedido_id), None)
        if pedido is None:
            raise ToolError("Esse pedido não existe nesta obra. Use listar_pedidos para achar o pedido_id certo.")
        return pedido

    @beta_async_tool(eager_input_streaming=True)
    async def propor_entrega(
        obra_id: str, pedido_id: str, linhas: list[LinhaEntregue], justificativa: str,
        nf_numero: str | None = None, nf_data: str | None = None, nf_valor: str | None = None,
        confirmar_excesso: bool = False,
    ) -> str:
        """Propõe registrar uma entrega que chegou num pedido: dá ENTRADA no almoxarifado do que chegou, com a nota.
        NÃO registra: cria uma proposta; aprovada, faz o mesmo que "Registrar entrega" na tela (e tem Desfazer).

        Use quando a pessoa disser que chegou material ou pedir para dar entrada de uma NF. Antes, ache o pedido com
        listar_pedidos (abertos=true, busca = fornecedor ou insumo) e a linha (indice) com o que falta_entregar. Serve
        para pedido parcial (o resto que chegou) e para pedido a caminho. Pergunte a quantidade se a pessoa não disse.
        A nota tem número, data e valor: passe os que a pessoa disser. Se a quantidade passar do que falta, confirme
        com a pessoa antes e só então mande confirmar_excesso=true. Para ENCERRAR um pedido sem entrada (o fornecedor
        não vai mandar o resto) use propor_avanco_pedido, não esta.

        Args:
            obra_id: ID da obra do pedido.
            pedido_id: ID do pedido (de listar_pedidos), não o número PED-NNNN.
            linhas: O que chegou, por linha do pedido: indice (de listar_pedidos) e quantidade na unidade do insumo,
                sem separador de milhar e com vírgula decimal ("7", "1,5").
            justificativa: Uma frase com o que a pessoa disse (ex.: "chegaram hoje os 7 m³ restantes").
            nf_numero: Número da nota fiscal, como a pessoa disse.
            nf_data: Data da nota, dd/mm/aaaa.
            nf_valor: Valor da nota em reais, como a pessoa disse ("910", "910,00").
            confirmar_excesso: true só depois de a pessoa confirmar que chegou mais do que faltava.
        """
        eventos.ferramenta("propor_entrega", "preparou uma proposta de entrada de entrega")
        if not linhas:
            raise ToolError("Diga o que chegou: ao menos uma linha com indice e quantidade.")
        entregas = []
        for x in linhas:
            q = _ler_qtd(str(x["quantidade"]), "quantidade")
            if q <= 0:
                raise ToolError("A quantidade entregue tem de ser maior que zero.")
            entregas.append({"indice": int(x["indice"]), "quantidade": q})
        pedido = await _pedido_da_obra(obra_id, pedido_id)
        do_pedido = _linhas(pedido)
        partes = []
        for e in entregas:
            if not 0 <= e["indice"] < len(do_pedido):
                raise ToolError(f"{pedido.get('numero')} não tem a linha {e['indice']}: use o indice de "
                                "listar_pedidos.")
            x = do_pedido[e["indice"]]
            partes.append(f"{_qtd_un(e['quantidade'], x.get('unidade', ''))} de {x.get('nome') or 'item'}")
        dados: dict[str, Any] = {"pedido_id": pedido_id, "linhas": entregas}
        nf = []
        if nf_numero and nf_numero.strip():
            dados["nf_numero"] = nf_numero.strip()
            nf.append(f"NF {nf_numero.strip()}")
        if nf_data:
            dados["nf_data"] = _data_iso(nf_data)
            nf.append(fmt_data(dados["nf_data"]))
        if nf_valor:
            dados["nf_valor"] = _ler_reais(nf_valor)
            nf.append(fmt_reais(dados["nf_valor"]))
        if confirmar_excesso:
            dados["confirmar_excesso"] = True
        return await _propor({
            "obra_id": obra_id,
            "tipo": "registrar_entrega",
            "titulo": f"{pedido.get('numero')}: dar entrada de {'; '.join(partes)}" + (f" ({', '.join(nf)})" if nf
                                                                                      else ""),
            "justificativa": justificativa,
            # a conta da entrega ("recebido 18 + esta entrega 7 = 25 de 25") a API monta, pelo domínio
            "conta": "",
            "fonte": f"pedido {pedido.get('numero')} ({pedido.get('fornecedor')})",
            "dados": dados,
        })

    @beta_async_tool(eager_input_streaming=True)
    async def propor_avanco_pedido(obra_id: str, pedido_id: str, para: StatusTexto, justificativa: str) -> str:
        """Propõe mudar o status de um pedido, SEM entrada de material. NÃO muda: cria uma proposta para aprovação.

        Use para: aprovado → enviado → em_transito, marcar que chegou para conferir ("recebido"), cancelar, ou ENCERRAR
        um pedido parcial (para = "conferido" num pedido "parcial": o fornecedor não vai mandar o resto, que sai do "a
        caminho" e volta como falta; nada entra no almoxarifado). Material que chegou NÃO é aqui: use propor_entrega.
        Ache o pedido_id com listar_pedidos antes.

        Args:
            obra_id: ID da obra do pedido.
            pedido_id: ID do pedido (de listar_pedidos), não o número PED-NNNN.
            para: Status de destino.
            justificativa: Uma frase com o motivo (ex.: "a pedreira avisou que não manda o resto").
        """
        eventos.ferramenta("propor_avanco_pedido", f"preparou uma proposta de mudança de status do pedido ({para})")
        pedido = await _pedido_da_obra(obra_id, pedido_id)
        de = pedido["status"]
        try:
            validar_transicao(StatusPedido(de), StatusPedido(para))
        except RegraViolada as e:
            raise ToolError(str(e)) from None
        numero = pedido.get("numero")
        if para == "conferido" and de != "parcial":
            raise ToolError(f"{numero} está {de}: conferir aqui daria entrada de tudo o que falta. Para registrar o "
                            "que chegou, use propor_entrega com as quantidades e a NF.")
        if para == "conferido":
            resto = [f"{_qtd_un(x['falta_entregar'], x.get('unidade', ''))} de {x.get('nome')}"
                     for x in _linhas(pedido) if x.get("falta_entregar")]
            titulo = f"{numero}: encerrar sem esperar o resto" + (f" ({'; '.join(resto)} não vêm)" if resto else "") \
                + " · sem entrada no almoxarifado"
        elif para == "cancelado":
            titulo = f"{numero}: cancelar o pedido"
        else:
            nomes = {"aprovado": "aprovado", "enviado": "enviado", "em_transito": "em trânsito",
                     "recebido": "chegou, falta conferir"}
            titulo = f"{numero}: marcar como {nomes.get(para, para)}"
        return await _propor({
            "obra_id": obra_id,
            "tipo": "avancar_pedido",
            "titulo": titulo,
            "justificativa": justificativa,
            "conta": "",
            "fonte": f"pedido {numero} ({pedido.get('fornecedor')})",
            "dados": {"pedido_id": pedido_id, "para": para},
        })

    @beta_async_tool(eager_input_streaming=True)
    async def propor_ajuste_estoque(obra_id: str, insumo_id: str, motivo: str, contagem: str | None = None,
                                    quantidade: str | None = None) -> str:
        """Propõe um ajuste de contagem no saldo de um insumo. NÃO ajusta: cria uma proposta para aprovação.

        Use quando a contagem física não bate com o sistema (quebra, perda, sobra achada). Quando a pessoa disser
        quanto CONTOU ("contei 8 m³"), mande contagem: a diferença contra o saldo do sistema é calculada aqui, não por
        você. Mande quantidade só quando ela disser a diferença ("sumiram 2 sacos"). Não use para entrada de compra
        (isso é propor_entrega) nem para baixa de consumo.

        Args:
            obra_id: ID da obra.
            insumo_id: ID do insumo (de consultar_estoque).
            motivo: Por que ajustar (ex.: "contagem física no canteiro", "190 blocos quebrados na descarga").
            contagem: Quanto a pessoa contou no canteiro, na unidade do insumo, com vírgula decimal ("8", "1,5").
            quantidade: Só se a pessoa disse a diferença: com sinal, "-190" tira, "35" põe. Vírgula decimal.
        """
        eventos.ferramenta("propor_ajuste_estoque", "preparou uma proposta de ajuste de estoque")
        if (contagem is None) == (quantidade is None):
            raise ToolError("Mande contagem (o que a pessoa contou) ou quantidade (a diferença), um dos dois.")
        item = await _item_do_insumo(obra_id, insumo_id)
        un = item["insumo"].get("unidade", "")
        conta = linha_de_conta(item["conta"], un)
        if contagem is not None:
            contado = _ler_qtd(contagem, "contagem")
            if contado < 0:
                raise ToolError("A contagem não pode ser negativa.")
            # a diferença sai da calculadora (Decimal), com a conta à mostra: "8 m³ − 10 m³ = −2 m³"
            saldo = item["conta"]["saldo"]
            r = _calcular(f"{fmt_qtd(contado).replace('.', '')} - {fmt_qtd(saldo).replace('.', '')}", un or "un")
            q = int(r.valor * 1000)
            conta = f"contagem {_qtd_un(contado, un)} − saldo no sistema {_qtd_un(saldo, un)} = ajuste " \
                    f"{r.resultado if un else r.resultado.removesuffix(' un')}"
        else:
            q = _ler_qtd(quantidade, "quantidade")
        if q == 0:
            raise ToolError("Ajuste de zero não muda nada: a contagem bate com o saldo do sistema.")
        sinal = "+" if q > 0 else "−"
        return await _propor({
            "obra_id": obra_id,
            "tipo": "ajuste_estoque",
            "titulo": f"Ajustar {item['insumo']['nome']} em {sinal}{_qtd_un(abs(q), un)}",
            # O motivo do ajuste é a justificativa da proposta; também vai em `dados`, que a API usa ao executar.
            "justificativa": motivo,
            "conta": conta,
            "fonte": "almoxarifado da obra (conta calculada pelo sistema)",
            "dados": {"insumo_id": insumo_id, "quantidade": q, "motivo": motivo},
        })

    @beta_async_tool(eager_input_streaming=True)
    async def consultar_orcamento(obra_id: str, busca: str | None = None) -> str:
        """Mostra o orçamento da obra e o previsto × realizado por etapa, com as contas já feitas pelo sistema.

        Traz: valor do contrato, aditivos (total, % sobre o contrato, cada aditivo com nº, descrição e data), orçado
        atual, e por etapa: orçado, em cotação, comprometido (pedidos), realizado (compras e serviços recebidos) e saldo
        a comprometer. Use para "quanto custou o aditivo", "a fundação estourou?", "quanto já gastamos", e para achar a
        chave da etapa antes de propor um pedido. Orçado é preço de contrato (com BDI e mão de obra); realizado é custo
        de compra, então realizado não é o % executado (para isso, consultar_cronograma).

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
            "fonte": "orçamento da obra (versões vigentes) e pedidos",
            "tem_orcamento": contrato is not None,
            "contratado": fmt_reais(t["contratado"]),
            "aditivos": fmt_reais(t["aditivos"]),
            "aditivos_pct_sobre_contrato": _pct(t.get("aditivos_pct")),
            "orcado_atual": fmt_reais(t["atual"]),
            "em_cotacao": fmt_reais(t["cotando"]),
            "comprometido": fmt_reais(t["comprometido"]),
            "realizado_compras": fmt_reais(t["realizado"]),
            "saldo_a_comprometer": _reais_ou_none(t.get("saldo")),
            "lista_aditivos": [{"numero": v["numero"], "descricao": v.get("descricao"), "data": fmt_data(v.get("data")),
                                "valor": fmt_reais(v["total"]), "itens": v["itens"]} for v in aditivos],
            "etapas": [{
                "etapa": e["etapa"], "descricao": e["descricao"], "orcado_atual": fmt_reais(e["atual"]),
                "aditivos": fmt_reais(e["aditivos"]), "em_cotacao": fmt_reais(e["cotando"]),
                "comprometido": fmt_reais(e["comprometido"]), "realizado_compras": fmt_reais(e["realizado"]),
                "saldo": _reais_ou_none(e.get("saldo")),
                "estourou": e.get("estourou"),
            } for e in o["etapas"]],
        }
        if t["sem_etapa"]["comprometido"] or t["sem_etapa"]["cotando"]:
            saida["sem_etapa"] = {"comprometido": fmt_reais(t["sem_etapa"]["comprometido"]),
                                  "realizado_compras": fmt_reais(t["sem_etapa"]["realizado"])}
        if busca:
            alvo = _sem_acento(busca)
            achados = [x for x in o["itens"] if x["tipo"] == "item" and alvo in _sem_acento(x["descricao"])]
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
        if mes:
            alvo = _mes_iso(mes)
            if alvo is None:
                raise ToolError(f"Mês '{mes}' não entendido: use 2026-12 ou dez/2026.")
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

        Traz: situação da obra; prazo (término, atraso contra o atual e o original, % medido × previsto); etapas em
        execução no mês, a próxima a começar e as atrasadas; financeiro (orçado atual, comprometido, realizado, saldo,
        aditivos); propostas aguardando aprovação; entregas de hoje, atrasadas e da semana; insumos com falta para
        comprar. Cada bloco diz quando o dado não existe (ex.: cronograma não cadastrado).

        Args:
            obra_id: ID da obra.
        """
        eventos.ferramenta("resumo_obra", "consultou o resumo da obra")
        try:
            r = await api.resumo_obra(obra_id)
        except ErroApi as e:
            raise _erro(e) from None
        return _json(_resumo(r))

    @beta_async_tool(eager_input_streaming=True)
    async def calcular(expressao: str, unidade_resultado: str | None = None) -> str:
        """A calculadora. Toda conta que não veio pronta de outra ferramenta passa por aqui; nunca faça conta de
        cabeça. Devolve a conta formatada para mostrar à pessoa como veio ("7 m³ × R$ 130,00/m³ = R$ 910,00").

        Os números têm de vir de uma ferramenta ou da pessoa. Use para: valor total a partir do preço unitário (para
        propor_pedido), custo de comprar mais ("10 sacos × último preço"), quanto falta medir (total − medido), soma de
        pedidos. Não use para refazer uma conta que a ferramenta já trouxe pronta (falta, saldo, atraso).

        Args:
            expressao: Só números, + − × ÷ (ou * /) e parênteses. Números com vírgula decimal e sem separador de
                milhar ("130,00", "2094833,38"). Depois de cada número, opcional, a unidade entre colchetes:
                "7 [m³] × 130,00 [R$/m³]", "2094833,38 [R$] − 82872,85 [R$]".
            unidade_resultado: Unidade do resultado: "R$" (arredonda ao centavo) ou a do insumo ("m³", "sc").
        """
        eventos.ferramenta("calcular", "fez uma conta")
        try:
            r = _calcular(expressao, unidade_resultado)
        except ErroConta as e:
            raise ToolError(f"Conta não feita: {e}") from None
        return _json({"conta": r.conta, "resultado": r.resultado,
                      "aviso": "Mostre a conta como veio, numa linha só dela."})

    @beta_async_tool(eager_input_streaming=True)
    async def listar_fornecedores(busca: str | None = None) -> str:
        """Lista os fornecedores cadastrados (valem para todas as obras), com CNPJ, contato e telefone.

        Use para achar o nome certo do fornecedor antes de propor um pedido, ou quando perguntarem o contato dele.

        Args:
            busca: Parte do nome para filtrar. Omita para todos.
        """
        eventos.ferramenta("listar_fornecedores", "consultou os fornecedores")
        try:
            fs = await api.fornecedores()
        except ErroApi as e:
            raise _erro(e) from None
        if busca:
            alvo = _sem_acento(busca)
            fs = [f for f in fs if alvo in _sem_acento(f["nome"])]
        return _json([{"nome": f["nome"], "cnpj": f.get("cnpj") or None, "contato": f.get("contato") or None,
                       "telefone": f.get("telefone") or None, "email": f.get("email") or None} for f in fs])

    return [
        listar_obras,
        resumo_obra,
        consultar_estoque,
        historico_movimentos,
        listar_pedidos,
        consultar_orcamento,
        consultar_cronograma,
        listar_fornecedores,
        calcular,
        _com_chamada(propor_pedido),
        _com_chamada(propor_entrega),
        _com_chamada(propor_avanco_pedido),
        _com_chamada(propor_ajuste_estoque),
    ]
