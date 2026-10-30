"""As 7 ferramentas do contrato. Cada uma chama a API central com o token do usuário.

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
from canteiro_dominio.regras import RegraViolada, validar_transicao
from canteiro_dominio.tipos import fmt_qtd, fmt_reais, qtd, reais

from .api_central import ApiCentral, ErroApi

Nivel = Literal["consulta", "propoe"]
NIVEIS: dict[str, Nivel] = {
    "listar_obras": "consulta",
    "consultar_estoque": "consulta",
    "historico_movimentos": "consulta",
    "listar_pedidos": "consulta",
    "propor_pedido": "propoe",
    "propor_avanco_pedido": "propoe",
    "propor_ajuste_estoque": "propoe",
}

StatusTexto = Literal["em_cotacao", "aprovado", "enviado", "em_transito", "recebido", "conferido", "cancelado"]


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


def _pedido(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "pedido_id": p["id"],
        "numero": p.get("numero"),
        "insumo_id": p.get("insumo_id"),
        "status": p.get("status"),
        "fornecedor": p.get("fornecedor"),
        "quantidade_milesimos": p.get("quantidade"),
        "quantidade": fmt_qtd(p.get("quantidade", 0)),
        "valor_centavos": p.get("valor"),
        "valor": fmt_reais(p.get("valor", 0)),
        "previsao": p.get("previsao"),
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
        """Lista as obras que a pessoa pode ver, com id, nome, cidade, status e verba.

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
                "verba_centavos": o.get("verba", 0),
                "verba": fmt_reais(o.get("verba", 0)),
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
        return _json({"obra_id": obra_id, "fonte": "almoxarifado da obra", "insumos": [_item_estoque(i) for i in itens]})

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
                "origem_doc": m.get("origem_doc"),
                "quem": m.get("quem"),
                "origem": m.get("origem"),
                "quando": m.get("criado_em"),
            }
            for m in movs
        ])

    @beta_async_tool(eager_input_streaming=True)
    async def listar_pedidos(obra_id: str, status: StatusTexto | None = None) -> str:
        """Lista os pedidos de compra de uma obra com número (PED-NNNN), status, fornecedor, quantidade e valor.

        Use para "o que está a caminho", "o pedido do aço já chegou?", ou para achar o pedido_id antes de propor um
        avanço. Não use para a falta de material: consultar_estoque já desconta o que está pedido.

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
        obra_id: str, insumo_id: str, quantidade: str, fornecedor: str, valor: str, justificativa: str
    ) -> str:
        """Propõe um pedido de compra. NÃO compra: cria uma proposta que uma pessoa aprova ou recusa na tela.

        Use quando a pessoa pedir para comprar/pedir material e você já tiver insumo, quantidade, fornecedor e valor.
        Se faltar algum desses dados, pergunte antes. A conta de falta anexada vem do sistema, não de você.

        Args:
            obra_id: ID da obra.
            insumo_id: ID do insumo (de consultar_estoque).
            quantidade: Quantidade na unidade do insumo, sem separador de milhar e com vírgula decimal: "3200", "1,5".
            fornecedor: Nome do fornecedor, como a pessoa disse.
            valor: Valor total do pedido em reais, como a pessoa disse: "4720", "4.720,00", "1299,90".
            justificativa: Uma frase com o motivo (ex.: "repor a falta de aço para a laje do 2º pavimento").
        """
        eventos.ferramenta("propor_pedido", "preparou uma proposta de pedido")
        q, v = _ler_qtd(quantidade, "quantidade"), _ler_reais(valor)
        if q <= 0 or v <= 0:
            raise ToolError("Quantidade e valor têm de ser maiores que zero.")
        item = await _item_do_insumo(obra_id, insumo_id)
        un = item["insumo"].get("unidade", "")
        return await _propor({
            "obra_id": obra_id,
            "tipo": "criar_pedido",
            "titulo": f"Comprar {_qtd_un(q, un)} de {item['insumo']['nome']} de {fornecedor} por {fmt_reais(v)}",
            "justificativa": justificativa,
            "conta": linha_de_conta(item["conta"], un),
            "fonte": "almoxarifado da obra (conta calculada pelo sistema)",
            "dados": {"insumo_id": insumo_id, "quantidade": q, "fornecedor": fornecedor, "valor": v},
        })

    @beta_async_tool(eager_input_streaming=True)
    async def propor_avanco_pedido(obra_id: str, pedido_id: str, para: StatusTexto, justificativa: str) -> str:
        """Propõe mudar o status de um pedido (ex.: em_transito → recebido). NÃO muda: cria uma proposta para aprovação.

        Use quando a pessoa disser que o pedido foi aprovado, enviado, chegou, foi conferido ou deve ser cancelado.
        Ir para "conferido" dá entrada no almoxarifado quando aprovado. Ache o pedido_id com listar_pedidos antes.

        Args:
            obra_id: ID da obra do pedido.
            pedido_id: ID do pedido (de listar_pedidos), não o número PED-NNNN.
            para: Status de destino.
            justificativa: Uma frase com o motivo (ex.: "mestre confirmou a chegada do caminhão").
        """
        eventos.ferramenta("propor_avanco_pedido", f"preparou uma proposta de avanço de pedido para {para}")
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
        return await _propor({
            "obra_id": obra_id,
            "tipo": "avancar_pedido",
            "titulo": f"{pedido.get('numero')}: {pedido['status']} → {para}",
            "justificativa": justificativa,
            "conta": "",
            "fonte": f"pedido {pedido.get('numero')} ({pedido.get('fornecedor')})",
            "dados": {"pedido_id": pedido_id, "para": para},
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

    return [
        listar_obras,
        consultar_estoque,
        historico_movimentos,
        listar_pedidos,
        propor_pedido,
        propor_avanco_pedido,
        propor_ajuste_estoque,
    ]
