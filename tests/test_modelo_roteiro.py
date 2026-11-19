"""O servidor falso da Anthropic: o SDK de verdade consegue falar com ele e o roteiro segue a intenção."""
import re

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from canteiro_agente.modelo_roteiro import criar_app, intencao

from falsos import OBRA, OBRA_EMEI


@pytest.fixture
def com_roteiro(ambiente):
    def fazer():
        amb = ambiente([])
        amb.dep.cliente_plataforma = AsyncAnthropic(
            api_key="falsa", base_url="http://roteiro", max_retries=0,
            http_client=DefaultAsyncHttpxClient(transport=httpx2.ASGITransport(app=criar_app())),
        )
        return amb
    return fazer


def _ferramentas(eventos):
    return [d["nome"] for e, d in eventos if e == "ferramenta"]


def _texto(eventos):
    return "".join(d["delta"] for e, d in eventos if e == "texto")


@pytest.mark.parametrize("frase,esperado", [
    ("compra 30 sacos de cimento", "compra"), ("o pedido do aço chegou?", "compra"), ("tem estoque?", "compra"),
    ("quanto já gastei na fundação?", "orcamento"), ("estourou o orçamento?", "orcamento"),
    ("vou acompanhar a obra", "desconhecido"),
    ("e aí, como tá a obra?", "resumo"), ("me dá um resumo", "resumo"),
    ("quanto vou pagar de imposto?", "tributos"), ("e o INSS da obra?", "tributos"), ("e o IBS/CBS?", "tributos"),
    ("qual o CNO?", "tributos"), ("como fica a aferição?", "tributos"),
])
def test_intencao(frase, esperado):
    assert intencao(frase) == esperado


async def test_compra_responde_o_que_faz_no_lugar_sem_ferramenta(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("Falta cimento? Pede na Votorantim por R$ 1.360,00", obra_id=OBRA)
    assert _ferramentas(ev) == []
    assert not [e for e, _ in ev if e == "proposta"] and not amb.api.feitas("POST", "/propostas")
    t = _texto(ev)
    assert t.startswith("Eu não faço compras nem controlo estoque.") and "comprovante em Gastos" in t
    assert ev[-1][0] == "fim"


async def test_gasto_da_etapa_vem_do_orcamento(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("Quanto já gastei nas fundações?", obra_id=OBRA_EMEI)
    assert _ferramentas(ev) == ["consultar_orcamento"]
    t = _texto(ev)
    # vem do tool_result, não de constante
    assert "3 FUNDAÇÕES: gasto R$ 118.000,00 de R$ 116.246,82 orçados." in t
    assert "**Passou do orçado**: falta gastar -R$ 1.753,18." in t


async def test_gasto_da_obra_lista_as_etapas_que_passaram(com_roteiro):
    amb = com_roteiro()
    t = _texto(await amb.conversar("estourou alguma coisa no orçamento?", obra_id=OBRA_EMEI))
    assert "A obra gastou **R$ 118.300,00** de R$ 2.094.833,38 orçados" in t
    assert "- 3 FUNDAÇÕES: gasto R$ 118.000,00 de R$ 116.246,82" in t


async def test_sem_obra_em_foco_lista_obras_primeiro(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("quanto já gastei?")
    assert _ferramentas(ev) == ["listar_obras", "consultar_orcamento"]


async def test_outra_coisa_nao_sabe_e_uso_e_plausivel(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("qual a previsão do tempo?")
    assert _ferramentas(ev) == [] and "Não sei com segurança" in _texto(ev)
    [uso] = amb.api.feitas("POST", "/uso")
    c = uso.corpo
    assert c["tokens_cache_escrita"] > 500 and c["tokens_entrada"] > 0 and c["tokens_saida"] > 0
    assert c["custo_microdolares"] > 0


def test_modo_roteiro_pelo_ambiente_nao_usa_chave(monkeypatch):
    """MODELO_ROTEIRO=1: a demo hospedada sobe sem ANTHROPIC_API_KEY e o cliente aponta para o roteiro em processo."""
    from canteiro_agente.config import Config
    from canteiro_agente.main import _cliente_plataforma

    monkeypatch.setenv("MODELO_ROTEIRO", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = Config.do_ambiente()
    assert cfg.modelo_roteiro and cfg.anthropic_api_key is None
    cliente = _cliente_plataforma(cfg)
    assert cliente is not None and str(cliente.base_url).startswith("http://roteiro")


async def test_sse_entrega_o_markdown_intacto(com_roteiro):
    """Parágrafos, lista, negrito e a quebra depois do texto que antecede a ferramenta chegam byte a byte na tela,
    mesmo com o texto cortado em vários deltas (o corte pode cair no meio de um "**" ou de um "\\n\\n")."""
    amb = com_roteiro()
    ev = await amb.conversar("o orçamento estourou?", obra_id=OBRA_EMEI)
    deltas = [d["delta"] for e, d in ev if e == "texto"]
    assert len(deltas) > 2
    t = "".join(deltas)
    assert t.startswith("Vou conferir o orçamento.\n\nA obra gastou **R$ 118.300,00**")
    paragrafos = t.split("\n\n")
    itens = paragrafos[2].split("\n")
    assert len(itens) == 1 and all(i.startswith("- ") for i in itens)
    assert t.count("**") % 2 == 0 and "\n\n\n" not in t and not t.endswith("\n")


def test_instrucao_pede_markdown_e_e_estavel():
    """Sem data, ID ou nome: o prefixo (ferramentas + instrução) precisa ser byte a byte igual para o cache valer."""
    from canteiro_agente.instrucao import INSTRUCAO

    assert "linha em branco" in INSTRUCAO and "###" in INSTRUCAO and '"- "' in INSTRUCAO
    # o que o teste com o modelo real pediu: nome, calculadora, resumo primeiro; e o Plumo não compra
    assert INSTRUCAO.startswith("Você é o Plumo") and "Canteiro" not in INSTRUCAO
    assert "calcular" in INSTRUCAO and "não faz compras" in INSTRUCAO
    assert not re.search(r"propor_(pedido|entrega|avanco|ajuste)|almoxarifado|listar_pedidos", INSTRUCAO)
    assert "resumo_obra" in INSTRUCAO and "no FIM" in INSTRUCAO
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\{|\}", INSTRUCAO)


async def test_resumo_responde_curto_e_deixa_o_que_falta_para_o_fim(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("e aí, como tá a obra?", obra_id=OBRA_EMEI)
    assert _ferramentas(ev) == ["resumo_obra"]
    t = _texto(ev)
    paragrafos = t.split("\n\n")
    assert paragrafos[0] == ("A EMEI Santa Clara do Sul está **em dia**, com término em ago/2027 (4,0% medido × "
                             "3,9% previsto).")
    itens = paragrafos[1].split("\n")
    assert 1 <= len(itens) <= 5 and all(i.startswith("- ") for i in itens)
    assert "Atrasada: 1 SERVIÇOS PRELIMINARES: 88,86% medido de 100% previsto" in t
    assert "Financeiro: gasto R$ 118.300,00 de R$ 2.094.833,38 (falta gastar R$ 1.976.533,38)" in t
    assert "Passou do orçado: 3 FUNDAÇÕES (passou R$ 1.753,18 do orçado)" in t
    assert "resumo_obra" not in t and "2027-08" not in t


async def test_resumo_de_obra_sem_cronograma_nem_orcamento(com_roteiro):
    amb = com_roteiro()
    t = _texto(await amb.conversar("como tá a obra?", obra_id=OBRA))
    assert t.startswith("A Casa Vila Nova está em andamento.")
    assert t.endswith("Cronograma não cadastrado: cadastre em Cronograma para acompanhar prazo e etapas. "
                      "Orçamento não importado: importe em Orçamento para acompanhar custo por etapa.")
    assert "PED-" not in t and "comprar" not in t


async def test_data_de_hoje_vai_na_mensagem_e_nao_na_instrucao(ambiente):
    from datetime import datetime

    from canteiro_agente.conversa import FUSO, contexto_de_hoje

    from falsos import Resposta, texto

    assert contexto_de_hoje(datetime(2026, 11, 5, 17, 30, tzinfo=FUSO)) == (
        "(Hoje é quinta-feira, 05/11/2026, 17:30 no horário de Brasília.)")
    amb = ambiente([Resposta([texto("ok")])])
    await amb.conversar("oi", obra_id=OBRA)
    [p] = amb.anthropic.pedidos
    primeiro = p["messages"][-1]["content"][0]["text"]
    assert primeiro.startswith("(Hoje é ") and "horário de Brasília" in primeiro
    assert "Hoje é" not in str(p["system"]) and "Hoje é" not in str(p["tools"])


async def test_tributos_cita_as_linhas_e_a_economia(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("Quanto vou pagar de INSS nesta obra?", obra_id=OBRA)
    assert _ferramentas(ev) == ["consultar_tributos"]
    assert not amb.api.feitas("POST", "/propostas")
    t = _texto(ev)
    assert "**R$ 5.246,52**" in t
    assert "- INSS = R$ 14.256,85 × 36,8% = R$ 5.246,52" in t                 # a linha como a API montou
    assert "faltam R$ 3.480,00 de remuneração declarada" in t
    assert "cairia **R$ 1.472,00**" in t
    assert "IBS/CBS: Obra para uso próprio" in t
    assert "1 gasto(s) (R$ 700,00)" in t
    assert t.rstrip().endswith("confira com o contador antes de pagar ou declarar.")
