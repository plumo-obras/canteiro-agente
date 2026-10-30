"""O servidor falso da Anthropic: o SDK de verdade consegue falar com ele e o roteiro segue a intenção."""
import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from canteiro_agente.modelo_roteiro import criar_app, intencao
from falsos import OBRA


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
    assert "190 un foram quebra" in t and "15%" in t
    assert ev[-1][0] == "fim"


async def test_cimento_propoe_o_que_falta(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("Falta cimento? Pede na Votorantim por R$ 1.360,00", obra_id=OBRA)
    assert _ferramentas(ev) == ["consultar_estoque", "propor_pedido"]
    [p] = [d for e, d in ev if e == "proposta"]
    assert p["dados"]["quantidade"] == 34_000 and p["dados"]["valor"] == 136_000
    assert p["dados"]["fornecedor"] == "Votorantim"
    assert "aguardando aprovação" in _texto(ev)


async def test_aco_ja_em_cotacao_nao_pede(com_roteiro):
    amb = com_roteiro()
    ev = await amb.conversar("Preciso pedir aço?", obra_id=OBRA)
    assert _ferramentas(ev) == ["consultar_estoque"]
    assert "Os 600 kg já estão em cotação e a falta é 0 kg; não precisa pedir." in _texto(ev)
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
