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
    ("por que o saldo de blocos é 2.760?", "blocos"), ("tem 2.760 mesmo?", "blocos"),
    ("falta cimento?", "cimento"), ("e o AÇO?", "aco"), ("vou acompanhar a obra", "desconhecido"),
    ("e aí, como tá a obra?", "resumo"), ("me dá um resumo", "resumo"),
    ("quanto eu gasto se comprar 10 sacos de cimento a mais?", "custo"),
])
def test_intencao(frase, esperado):
    assert intencao(frase) == esperado


async def test_blocos_cita_conta_e_quebra_sem_propor(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("Por que o saldo de blocos está em 2.760?", obra_id=OBRA)
    assert _ferramentas(ev) == ["consultar_estoque", "historico_movimentos"]
    assert not [e for e, _ in ev if e == "proposta"] and not amb.api.feitas("POST", "/propostas")
    t = _texto(ev)
    assert "saldo 2.760 un" in t and "falta 2.040 un" in t      # vem do tool_result, não de constante
    assert "- 190 un foram quebra" in t and "**15%**" in t
    assert ev[-1][0] == "fim"


async def test_cimento_propoe_o_que_falta(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("Falta cimento? Pede na Votorantim por R$ 1.360,00", obra_id=OBRA)
    assert _ferramentas(ev) == ["consultar_estoque", "propor_pedido"]
    [p] = [d for e, d in ev if e == "proposta"]
    assert p["dados"]["quantidade"] == 34_000 and p["dados"]["valor"] == 136_000
    assert p["dados"]["fornecedor"] == "Votorantim"
    assert "**aguardando aprovação**" in _texto(ev)


async def test_aco_ja_em_cotacao_nao_pede(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("Preciso pedir aço?", obra_id=OBRA)
    assert _ferramentas(ev) == ["consultar_estoque"]
    assert "Os 600 kg já estão em cotação e a falta é **0 kg**: **não precisa pedir**." in _texto(ev)
    assert not [e for e, _ in ev if e == "proposta"]


async def test_sem_obra_em_foco_lista_obras_primeiro(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("e o aço?")
    assert _ferramentas(ev) == ["listar_obras", "consultar_estoque"]


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
    ev = await amb.conversar("Por que o saldo de blocos está em 2.760?", obra_id=OBRA)
    deltas = [d["delta"] for e, d in ev if e == "texto"]
    assert len(deltas) > 2
    t = "".join(deltas)
    assert t.startswith("Vou conferir o almoxarifado.\n\nBloco cerâmico 14x19x29, pelo almoxarifado:\n\n")
    paragrafos = t.split("\n\n")
    linha_conta = paragrafos[2]
    assert "saldo 2.760 un" in linha_conta and "falta 2.040 un" in linha_conta and "\n" not in linha_conta
    assert paragrafos[3] == "Das saídas registradas:"
    itens = paragrafos[4].split("\n")
    assert len(itens) == 3 and all(i.startswith("- ") for i in itens)
    assert t.count("**") % 2 == 0 and "\n\n\n" not in t and not t.endswith("\n")


async def test_texto_antes_da_ferramenta_nao_cola_no_seguinte(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("Falta cimento? Pede na Votorantim por R$ 1.360,00", obra_id=OBRA)
    t = _texto(ev)
    assert "Vou propor o pedido de **34 sc**.\n\nDeixei a proposta" in t
    assert "\n\nConta:\n\n" in t


def test_instrucao_pede_markdown_e_e_estavel():
    """Sem data, ID ou nome: o prefixo (ferramentas + instrução) precisa ser byte a byte igual para o cache valer."""
    from canteiro_agente.instrucao import INSTRUCAO

    assert "linha em branco" in INSTRUCAO and "###" in INSTRUCAO and '"- "' in INSTRUCAO
    # o que o teste com o modelo real pediu: nome, calculadora, sem previsão ≠ não precisa, resumo primeiro
    assert INSTRUCAO.startswith("Você é o Plumo") and "Canteiro" not in INSTRUCAO
    assert "calcular" in INSTRUCAO and "nunca quer dizer que a obra não precisa" in INSTRUCAO
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
    # o que falta cadastrar fecha a resposta, sem negrito
    assert "sem previsão" in paragrafos[-1] and "**" not in paragrafos[-1]
    assert "resumo_obra" not in t and "2027-08" not in t


async def test_resumo_de_obra_sem_cronograma_nem_orcamento(com_roteiro):
    amb = com_roteiro()
    t = _texto(await amb.conversar("como tá a obra?", obra_id=OBRA))
    assert t.startswith("A Casa Vila Nova está em andamento.")
    assert t.endswith("Cronograma não cadastrado: cadastre em Cronograma para acompanhar prazo e etapas. "
                      "Orçamento não importado: importe em Orçamento para acompanhar custo por etapa.")
    assert "atrasada PED-0158" in t


async def test_custo_usa_o_ultimo_preco_e_a_calculadora(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("quanto eu gasto se comprar 10 sacos de cimento a mais?", obra_id=OBRA_EMEI)
    assert _ferramentas(ev) == ["consultar_estoque", "calcular"]
    t = _texto(ev)
    assert "Dá **R$ 375,00** pelo último preço (PED-0169" in t
    assert "\n\n10 sc × R$ 37,50/sc = R$ 375,00\n\n" in t


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
