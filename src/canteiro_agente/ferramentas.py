"""As ferramentas do contrato. Cada uma chama a API central com o token do usuário.

Devolvem números brutos (milésimos, centavos, como a API manda) e formatados ("2.760 un", "R$ 472,00"), mais a
linha de conta pronta, para o modelo citar sem fazer conta. As de nível `propoe` só criam proposta (`POST /propostas`):
nada é executado sem a aprovação de uma pessoa.

As ferramentas são criadas por requisição (fecham sobre o token daquela conversa), mas nome, descrição e esquema são
sempre os mesmos bytes: é isso que mantém o cache do prefixo (ferramentas + instrução) válido entre conversas.
"""
import json
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from anthropic import beta_async_tool
from anthropic.lib.tools import ToolError

from canteiro_dominio.modelos import StatusPedido
from canteiro_dominio.regras import RegraViolada, validar_conferencia, validar_transicao
from canteiro_dominio.tipos import fmt_qtd, fmt_reais, qtd, reais

from .api_central import ApiCentral, ErroApi

Nivel = Literal["consulta", "propoe"]
NIVEIS: dict[str, Nivel] = {
    "listar_obras": "consulta",
    "consultar_estoque": "consulta",
    "historico_movimentos": "consulta",
    "listar_pedidos": "consulta",
    "consultar_orcamento": "consulta",
    "consultar_cronograma": "consulta",
    "listar_fornecedores": "consulta",
    "propor_pedido": "propoe",
    "propor_avanco_pedido": "propoe",
    "propor_ajuste_estoque": "propoe",
}

StatusTexto = Literal["em_cotacao", "aprovado", "enviado", "em_transito", "recebido", "parcial", "conferido",
                      "cancelado"]


@dataclass
class Eventos:
    """Fila do que as ferramentas querem mostrar na tela (eventos `ferramenta` e `proposta` do SSE)."""
    pendentes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def ferramenta(self, nome: str, resumo: str) -> None:
        self.pendentes.append(("ferramenta", {"nome": nome, "nivel": NIVEIS[nome], "resumo": resumo}))

    def proposta(self, proposta: dict[str, Any]) -> None:
        self.pendentes.append(("proposta", proposta))

    def esvaziar(self) -> list[tuple[str, dict[str, Any]]]:
        saida, self.pendentes = self.pendentes, []
        return saida


def _json(dado: Any) -> str:
    return json.dumps(dado, ensure_ascii=False)


def _sem_acento(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t.casefold()) if unicodedata.category(c) != "Mn")


def _erro(e: ErroApi) -> ToolError:
    if e.status in (401, 403):
        return ToolError(f"Sem permissão para isso (HTTP {e.status}): {e.mensagem}")
    if e.status == 404:
        return ToolError(f"Não encontrado: {e.mensagem}")
    return ToolError(f"A API recusou (HTTP {e.status}): {e.mensagem}")


def _qtd_un(m: int, unidade: str) -> str:
    return f"{fmt_qtd(m)} {unidade}".strip()


def linha_de_conta(conta: dict[str, int], unidade: str) -> str:
    """Só formata os números que a API calculou (regras.conta_de_falta). Nenhuma operação aqui."""
    f = lambda k: _qtd_un(conta.get(k, 0), unidade)  # noqa: E731
    return (
        f"precisa {f('precisa')} − saldo {f('saldo')} − a caminho {f('a_caminho')} − em cotação {f('cotando')}"
        f" − transferência {f('transferencia')} = falta {f('falta')}"
    )


def _item_estoque(item: dict[str, Any]) -> dict[str, Any]:
    insumo, conta = item["insumo"], item["conta"]
    un = insumo.get("unidade", "")
    return {
        "insumo_id": insumo["id"],
        "nome": insumo["nome"],
        "unidade": un,
        "curva": insumo.get("curva"),
        "conta_milesimos": conta,
        "conta_formatada": {k: _qtd_un(v, un) for k, v in conta.items()},
        "linha_conta": linha_de_conta(conta, un),
    }


def _linhas(p: dict[str, Any]) -> list[dict[str, Any]]:
    # pedido de antes das linhas (um insumo só) ainda pode aparecer em dado antigo
    if p.get("linhas"):
        return p["linhas"]
    return [{"insumo_id": p.get("insumo_id"), "quantidade": p.get("quantidade", 0), "total": p.get("valor", 0),
             "recebida": p.get("quantidade_recebida") or 0, "nome": "", "unidade": ""}]


def _pedido(p: dict[str, Any]) -> dict[str, Any]:
    linhas = []
    for x in _linhas(p):
        un = x.get("unidade", "")
        falta = max(0, x.get("quantidade", 0) - x.get("recebida", 0))
        linhas.append({
            "insumo_id": x.get("insumo_id"),
            "nome": x.get("nome"),
            "tipo": x.get("tipo", "material"),
            "etapa": x.get("etapa"),
            "quantidade": _qtd_un(x.get("quantidade", 0), un),
            "recebida": _qtd_un(x.get("recebida", 0), un),
            "falta_entregar": _qtd_un(falta, un),
            "falta_entregar_milesimos": falta,
            "total": fmt_reais(x.get("total", 0)),
        })
    return {
        "pedido_id": p["id"],
        "numero": p.get("numero"),
        "status": p.get("status"),
        "fornecedor": p.get("fornecedor"),
        "valor_centavos": p.get("valor"),
        "valor": fmt_reais(p.get("valor", 0)),
        "previsao": p.get("previsao"),
        "entregas": p.get("recebimentos", 0),
        "linhas": linhas,
    }


def _pct(c: int | None) -> str | None:
    return None if c is None else f"{c / 100:.1f}".replace(".", ",") + "%"


def _meses(decimos: int | None) -> str | None:
    if decimos is None:
        return None
    if decimos == 0:
        return "em dia"
    n = abs(decimos) / 10
    t = (f"{n:.1f}".replace(".", ",") if decimos % 10 else f"{n:.0f}") + (" mês" if n <= 1 else " meses")
    return f"{t} de atraso" if decimos > 0 else f"adiantada {t}"


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
        try:
            proposta = await api.criar_proposta(corpo)
        except ErroApi as e:
            raise _erro(e) from None
        eventos.proposta(proposta)
        return _json({
            "proposta_id": proposta.get("id"),
            "status": proposta.get("status", "pendente"),
            "titulo": proposta.get("titulo"),
            "aviso": "Proposta criada e aguardando aprovação de uma pessoa. Nada foi executado ainda.",
        })

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
                "inicio": o.get("inicio"),
                "prazo_meses": o.get("prazo_meses") or None,
            }
            for o in obras
        ])

    @beta_async_tool(eager_input_streaming=True)
    async def consultar_estoque(obra_id: str, busca: str | None = None) -> str:
        """Mostra os insumos do almoxarifado de uma obra com a conta de falta já calculada pelo sistema.

        Cada insumo traz precisa, saldo, a caminho, em cotação, transferência, falta e comprar (brutos em milésimos e
        formatados) e a linha_conta pronta para citar. Use para "quanto tem", "quanto falta", "precisa comprar?".
        Não calcule nada a partir desses números: cite a linha_conta.

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
                "quando": m.get("criado_em"),
            }
            for m in movs
        ])

    @beta_async_tool(eager_input_streaming=True)
    async def listar_pedidos(obra_id: str, status: StatusTexto | None = None) -> str:
        """Lista os pedidos de compra de uma obra: número (PED-NNNN), status, fornecedor, previsão, valor e as linhas
        (insumo, etapa do orçamento, quantidade pedida, recebida e o que falta entregar).

        Use para "o que está a caminho", "o pedido do aço já chegou?", "falta chegar brita?", ou para achar o pedido_id
        antes de propor um avanço. Status "parcial" = chegou parte, o resto continua a caminho. Não use para a falta de
        material: consultar_estoque já desconta o que está pedido.

        Args:
            obra_id: ID da obra.
            status: Filtra por um status. Omita para todos.
        """
        eventos.ferramenta("listar_pedidos", f"consultou os pedidos{f' ({status})' if status else ''}")
        try:
            pedidos = await api.pedidos(obra_id, status)
        except ErroApi as e:
            raise _erro(e) from None
        return _json([_pedido(p) for p in pedidos])

    @beta_async_tool(eager_input_streaming=True)
    async def propor_pedido(
        obra_id: str, insumo_id: str, quantidade: str, fornecedor: str, valor: str, justificativa: str,
        etapa: str | None = None, previsao: str | None = None,
    ) -> str:
        """Propõe um pedido de compra. NÃO compra: cria uma proposta que uma pessoa aprova ou recusa na tela.

        Use quando a pessoa pedir para comprar/pedir material e você já tiver insumo, quantidade, fornecedor e valor.
        Se faltar algum desses dados, pergunte antes. Aprovada, a proposta vira um pedido já aprovado (a caminho), sem
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
            previsao: Data prevista de entrega "AAAA-MM-DD", se a pessoa disse.
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
            dados["previsao"] = previsao.strip()
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

    @beta_async_tool(eager_input_streaming=True)
    async def propor_avanco_pedido(
        obra_id: str, pedido_id: str, para: StatusTexto, justificativa: str,
        quantidade_recebida: str | None = None, nf: str | None = None,
    ) -> str:
        """Propõe mudar o status de um pedido (ex.: em_transito → recebido). NÃO muda: cria uma proposta para aprovação.

        Use quando a pessoa disser que o pedido foi aprovado, enviado, chegou, foi conferido ou deve ser cancelado.
        Ir para "conferido" dá entrada no almoxarifado quando aprovado. Ache o pedido_id com listar_pedidos antes.
        Ao conferir, se a pessoa disser que chegou quantidade diferente da pedida, informe quantidade_recebida (entra
        o que chegou; o resto continua a caminho, status parcial). Se ela disser o número da nota, informe nf. Pedido
        "parcial" → "conferido" = encerrar o resto, quando o fornecedor não vai mandar.

        Args:
            obra_id: ID da obra do pedido.
            pedido_id: ID do pedido (de listar_pedidos), não o número PED-NNNN.
            para: Status de destino.
            justificativa: Uma frase com o motivo (ex.: "mestre confirmou a chegada do caminhão").
            quantidade_recebida: Só com para = "conferido": o que chegou de fato, na unidade do insumo, com vírgula
                decimal ("36", "1,5"). Omita se chegou exatamente o pedido.
            nf: Só com para = "conferido": número da nota fiscal da entrega, como a pessoa disse.
        """
        eventos.ferramenta("propor_avanco_pedido", f"preparou uma proposta de avanço de pedido para {para}")
        q = _ler_qtd(quantidade_recebida, "quantidade_recebida") if quantidade_recebida else None
        nf = (nf or "").strip() or None
        try:
            validar_conferencia(StatusPedido(para), quantidade_recebida=q, nf=nf)
        except RegraViolada as e:
            raise ToolError(str(e)) from None
        try:
            pedidos = await api.pedidos(obra_id)
        except ErroApi as e:
            raise _erro(e) from None
        pedido = next((p for p in pedidos if p["id"] == pedido_id), None)
        if pedido is None:
            raise ToolError("Esse pedido não existe nesta obra. Use listar_pedidos para achar o pedido_id certo.")
        try:
            validar_transicao(StatusPedido(pedido["status"]), StatusPedido(para))
        except RegraViolada as e:
            raise ToolError(str(e)) from None
        linhas = _linhas(pedido)
        if q is not None and len(linhas) > 1:
            raise ToolError(f"{pedido.get('numero')} tem {len(linhas)} linhas: a entrega parcial de cada linha é "
                            "registrada na tela (Suprimentos → pedido → Registrar entrega). Oriente a pessoa.")
        titulo = f"{pedido.get('numero')}: {pedido['status']} → {para}"
        if pedido["status"] == "parcial" and para == "conferido":
            titulo = f"{pedido.get('numero')}: encerrar o resto (o fornecedor não manda)"
        if q is not None and q != linhas[0].get("quantidade"):
            un = linhas[0].get("unidade") or (await _item_do_insumo(obra_id, linhas[0]["insumo_id"]))["insumo"].get(
                "unidade", "")
            titulo += f" · chegaram {fmt_qtd(q)} de {_qtd_un(linhas[0].get('quantidade', 0), un)}"
        if nf:
            titulo += f" (NF {nf})"
        dados: dict[str, Any] = {"pedido_id": pedido_id, "para": para}
        if q is not None:
            dados["quantidade_recebida"] = q
        if nf:
            dados["nf"] = nf
        return await _propor({
            "obra_id": obra_id,
            "tipo": "avancar_pedido",
            "titulo": titulo,
            "justificativa": justificativa,
            "conta": "",
            "fonte": f"pedido {pedido.get('numero')} ({pedido.get('fornecedor')})",
            "dados": dados,
        })

    @beta_async_tool(eager_input_streaming=True)
    async def propor_ajuste_estoque(obra_id: str, insumo_id: str, quantidade: str, motivo: str) -> str:
        """Propõe um ajuste de contagem no saldo de um insumo. NÃO ajusta: cria uma proposta para aprovação.

        Use quando a contagem física não bate com o sistema (quebra, perda, sobra achada). Não use para entrada de
        compra (isso é avançar o pedido para conferido) nem para baixa de consumo.

        Args:
            obra_id: ID da obra.
            insumo_id: ID do insumo (de consultar_estoque).
            quantidade: Diferença a ajustar na unidade do insumo, com sinal: "-190" tira, "35" põe. Vírgula decimal.
            motivo: Por que ajustar (ex.: "190 blocos quebrados na descarga").
        """
        eventos.ferramenta("propor_ajuste_estoque", "preparou uma proposta de ajuste de estoque")
        q = _ler_qtd(quantidade, "quantidade")
        if q == 0:
            raise ToolError("Ajuste de zero não muda nada.")
        item = await _item_do_insumo(obra_id, insumo_id)
        un = item["insumo"].get("unidade", "")
        sinal = "+" if q > 0 else "−"
        return await _propor({
            "obra_id": obra_id,
            "tipo": "ajuste_estoque",
            "titulo": f"Ajustar {item['insumo']['nome']} em {sinal}{_qtd_un(abs(q), un)}",
            # O motivo do ajuste é a justificativa da proposta; também vai em `dados`, que a API usa ao executar.
            "justificativa": motivo,
            "conta": linha_de_conta(item["conta"], un),
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
        saida: dict[str, Any] = {
            "obra_id": obra_id,
            "fonte": "orçamento da obra (versões vigentes) e pedidos",
            "tem_orcamento": contrato is not None,
            "contratado": fmt_reais(t["contratado"]),
            "aditivos": fmt_reais(t["aditivos"]),
            "aditivos_pct_sobre_contrato": _pct(round(t["aditivos"] * 10000 / t["contratado"]))
            if t["contratado"] else None,
            "orcado_atual": fmt_reais(t["atual"]),
            "em_cotacao": fmt_reais(t["cotando"]),
            "comprometido": fmt_reais(t["comprometido"]),
            "realizado_compras": fmt_reais(t["realizado"]),
            "saldo_a_comprometer": fmt_reais(t["atual"] - t["comprometido"]),
            "lista_aditivos": [{"numero": v["numero"], "descricao": v.get("descricao"), "data": v.get("data"),
                                "valor": fmt_reais(v["total"]), "itens": v["itens"]} for v in aditivos],
            "etapas": [{
                "etapa": e["etapa"], "descricao": e["descricao"], "orcado_atual": fmt_reais(e["atual"]),
                "aditivos": fmt_reais(e["aditivos"]), "em_cotacao": fmt_reais(e["cotando"]),
                "comprometido": fmt_reais(e["comprometido"]), "realizado_compras": fmt_reais(e["realizado"]),
                "saldo": fmt_reais(e["atual"] - e["comprometido"]),
                "estourou": e["comprometido"] > e["atual"],
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
    async def consultar_cronograma(obra_id: str) -> str:
        """Mostra o cronograma físico-financeiro, as medições e o prazo projetado, com as contas feitas pelo sistema.

        Traz: término contratual e projetado, atraso contra o cronograma atual e contra o original (linha de base, que
        a reprogramação não apaga), % previsto × % medido na última medição, previsto e medido por mês e as medições.
        Use para "a obra fecha no prazo?", "estamos atrasados?", "quanto foi medido?". Se a obra foi reprogramada, o
        atraso que importa para o contrato é o contra o original; diga os dois.

        Args:
            obra_id: ID da obra.
        """
        eventos.ferramenta("consultar_cronograma", "consultou o cronograma e as medições")
        try:
            c = await api.cronograma(obra_id)
        except ErroApi as e:
            raise _erro(e) from None
        p = c["prazo"]
        if not c.get("inicio"):
            return _json({"obra_id": obra_id, "tem_cronograma": False,
                          "termino_contratual": p.get("termino_contratual"),
                          "aviso": "A obra ainda não tem cronograma no sistema: não há como projetar o prazo."})
        return _json({
            "obra_id": obra_id,
            "fonte": "cronograma físico-financeiro e medições da obra",
            "tem_cronograma": True,
            "inicio": c["inicio"], "meses": c["meses"],
            "termino_contratual": p.get("termino_contratual"),
            "termino_projetado": p.get("termino_projetado"),
            "situacao_contra_cronograma_atual": _meses(p.get("atraso_decimos")),
            "reprogramacoes": c.get("revisoes", 0),
            "cronograma_original": c.get("base"),
            "termino_original": p.get("termino_base"),
            "situacao_contra_original": _meses(p.get("atraso_base_decimos")),
            "mes_da_ultima_medicao": p.get("mes_referencia"),
            "pct_previsto_na_medicao": _pct(p.get("pct_previsto_na_medicao")),
            "pct_medido": _pct(p.get("pct_medido")),
            "sem_medicao": not c["medicoes"],
            "mensal": [{"mes": m["mes"], "previsto": fmt_reais(m["previsto"]),
                        "pct_previsto_acumulado": _pct(m["pct_previsto_acumulado"]),
                        "medido": None if m["medido"] is None else fmt_reais(m["medido"]),
                        "pct_medido_acumulado": _pct(m["pct_medido_acumulado"])} for m in c["mensal"]],
            "medicoes": [{"numero": m["numero"], "mes": m["mes"], "valor": fmt_reais(m["valor"]),
                          "pct_obra_acumulado": _pct(m["pct_obra_acumulado"]), "observacao": m.get("observacao")}
                         for m in c["medicoes"]],
        })

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
        consultar_estoque,
        historico_movimentos,
        listar_pedidos,
        consultar_orcamento,
        consultar_cronograma,
        listar_fornecedores,
        propor_pedido,
        propor_avanco_pedido,
        propor_ajuste_estoque,
    ]
