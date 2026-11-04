"""O que acontece quando a volta não termina bem: conexão caída, JSON quebrado, modelo sem preço, tempo esgotado,
limite de passos e o crescimento do contexto."""
import asyncio
import logging
from dataclasses import replace

import pytest

from canteiro_agente import conversa as mod
from canteiro_agente.conversa import (
    AVISO_LIMITE_PASSOS, AVISO_TEMPO, PedidoConversa, aguardar_pendentes, conversar,
)
from falsos import OBRA, TOKEN_USUARIO, Resposta, ferramenta, json_ilegivel, pausada, texto

PROPOSTA_CIMENTO = {
    "obra_id": OBRA, "insumo_id": "insumo-cimento", "quantidade": "34", "fornecedor": "Votorantim",
    "valor": "1.360,00", "justificativa": "repor a falta de cimento",
}


async def _cair_no_meio(amb, texto_pergunta="oi"):
    """Conversa direto no gerador e cancela a tarefa que o lê assim que chega o 1º texto, como o servidor faz quando
    a pessoa fecha a aba."""
    pronto = asyncio.Event()

    async def ler():
        async for nome, _ in conversar(amb.dep, TOKEN_USUARIO, PedidoConversa(texto_pergunta)):
            if nome == "texto":
                pronto.set()

    leitor = asyncio.create_task(ler())
    await asyncio.wait_for(pronto.wait(), 5)
    leitor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leitor
    await asyncio.wait_for(aguardar_pendentes(), 5)


# ---------------------------------------------------------------- 1. uso gravado mesmo com a conexão caída

async def test_conexao_caida_no_meio_grava_o_que_ja_foi_gasto(ambiente):
    amb = ambiente([pausada(Resposta([texto("Começando a resposta")], entrada=1000, cache_leitura=500), 30)])
    await _cair_no_meio(amb)

    [uso] = amb.api.feitas("POST", "/uso")
    c = uso.corpo
    # Até o message_start: entrada e cache completos, saída só a parcial que ele anuncia (1 token).
    assert (c["tokens_entrada"], c["tokens_cache_leitura"], c["tokens_saida"]) == (1000, 500, 1)
    assert c["custo_microdolares"] == 1000 * 4 + 500 * 0.20 + 1 * 20
    assert c["modelo"] == "claude-opus-5-5" and c["sessao_id"]


async def test_gerador_fechado_tambem_grava_o_uso(ambiente):
    amb = ambiente([pausada(Resposta([texto("Começando a resposta")], entrada=700), 30)])
    gen = conversar(amb.dep, TOKEN_USUARIO, PedidoConversa("oi"))
    async for nome, _ in gen:
        if nome == "texto":
            break
    await gen.aclose()
    await asyncio.wait_for(aguardar_pendentes(), 5)
    [uso] = amb.api.feitas("POST", "/uso")
    assert uso.corpo["tokens_entrada"] == 700


# ---------------------------------------------------------------- 4. histórico gravado a cada rodada de ferramenta

async def test_proposta_antes_da_queda_fica_no_historico_e_nao_se_repete(ambiente):
    amb = ambiente([
        Resposta([ferramenta("propor_pedido", PROPOSTA_CIMENTO)], stop_reason="tool_use", entrada=1000, saida=200),
        pausada(Resposta([texto("Deixei a proposta aguardando aprovação.")]), 30),
        Resposta([texto("A proposta de 34 sc já está aguardando aprovação.")]),
    ])
    await _cair_no_meio(amb, "Pede 34 sacos de cimento na Votorantim por 1.360")

    assert len(amb.api.feitas("POST", "/propostas")) == 1
    # A 1ª resposta foi registrada inteira uma vez; a 2ª, cortada, pelo que já tinha gastado. Nada em dobro.
    usos = [u.corpo for u in amb.api.feitas("POST", "/uso")]
    assert [(u["tokens_entrada"], u["tokens_saida"]) for u in usos] == [(1000, 200), (100, 1)]

    [sessao_id] = list(amb.dep.sessoes._dados)
    guardada = amb.dep.sessoes._dados[sessao_id]
    # Chamada e resultado juntos: nunca um tool_use sem o tool_result.
    assert [m["role"] for m in guardada.mensagens] == ["user", "assistant", "user"]
    assert guardada.mensagens[1]["content"][-1]["type"] == "tool_use"
    assert guardada.mensagens[2]["content"][0]["type"] == "tool_result"

    eventos = await amb.conversar("e o cimento?", sessao_id=sessao_id)
    assert [e for e, _ in eventos][-1] == "fim"
    ultimo = amb.anthropic.pedidos[-1]
    assert [m["role"] for m in ultimo["messages"]] == ["user", "assistant", "user", "user"]
    assert len(amb.api.feitas("POST", "/propostas")) == 1


# ---------------------------------------------------------------- 2. JSON de ferramenta ilegível

async def test_json_ilegivel_registra_a_tentativa_e_manda_descartar_o_texto(ambiente):
    amb = ambiente([json_ilegivel("Vou ver suas obras."), Resposta([texto("Uma obra: Casa Vila Nova.")])])
    eventos = await amb.conversar("quais obras?")

    nomes = [e for e, _ in eventos]
    assert ("descartar", {"texto": "Vou ver suas obras."}) in eventos
    i = nomes.index("descartar")
    antes = "".join(d["delta"] for e, d in eventos[:i] if e == "texto")
    depois = "".join(d["delta"] for e, d in eventos[i:] if e == "texto")
    assert antes == "Vou ver suas obras." and depois == "Uma obra: Casa Vila Nova."
    assert nomes[-1] == "fim" and "erro" not in nomes
    assert len(amb.anthropic.pedidos) == 2
    # A tentativa abortada também foi cobrada pela Anthropic: entra no livro.
    assert len(amb.api.feitas("POST", "/uso")) == 2
    fim = eventos[-1][1]
    assert fim["uso"]["tokens"] == (100 + 1) + (100 + 20)


async def test_outro_value_error_nao_vira_repeticao(ambiente, monkeypatch):
    def quebrado(self):
        raise ValueError("defeito nosso, não do JSON do modelo")

    monkeypatch.setattr(mod.Eventos, "esvaziar", quebrado)
    amb = ambiente([
        Resposta([texto("Vou ver."), ferramenta("listar_obras", {})], stop_reason="tool_use"),
        Resposta([texto("não devia chegar aqui")]),
    ])
    eventos = await amb.conversar("quais obras?")
    assert eventos[-1] == ("erro", {"mensagem": "Algo deu errado do nosso lado. Tente de novo."})
    assert len(amb.anthropic.pedidos) == 1


# ---------------------------------------------------------------- 3. modelo fora da tabela de preços

async def test_modelo_sem_preco_grava_no_preco_do_pedido_e_segue(ambiente, caplog):
    amb = ambiente([Resposta([texto("Resposta do resgate.")], modelo="claude-modelo-novo")])
    with caplog.at_level(logging.ERROR, logger="canteiro_agente"):
        eventos = await amb.conversar("oi")

    assert [e for e, _ in eventos] == ["texto", "texto", "fim"]
    [uso] = amb.api.feitas("POST", "/uso")
    assert uso.corpo["modelo"] == "claude-modelo-novo"
    assert uso.corpo["custo_microdolares"] == 100 * 4 + 20 * 20          # preço do claude-opus-5-5, o pedido
    assert eventos[-1][1]["uso"]["custo"] == 800
    [registro] = [r for r in caplog.records if "sem preço" in r.getMessage()]
    assert registro.levelno == logging.ERROR and uso.corpo["id"] in registro.getMessage()


# ---------------------------------------------------------------- 5. ping e tempo máximo

async def test_silencio_longo_manda_ping(ambiente):
    amb = ambiente([pausada(Resposta([texto("ok")]), 0.15)])
    amb.dep.config = replace(amb.dep.config, intervalo_ping=0.02)
    eventos = await amb.conversar("oi")
    assert amb.comentarios and set(amb.comentarios) == {": ping"}
    assert [e for e, _ in eventos][-1] == "fim"


async def test_volta_que_passa_do_tempo_termina_com_aviso_e_mantem_a_sessao(ambiente):
    amb = ambiente([pausada(Resposta([texto("Começando")], entrada=500), 30)])
    amb.dep.config = replace(amb.dep.config, tempo_max_volta=0.1)
    eventos = await amb.conversar("oi")

    assert [e for e, _ in eventos] == ["texto", "texto", "erro", "fim"]
    assert eventos[2][1]["mensagem"] == AVISO_TEMPO
    assert eventos[3][1]["sessao_id"]
    [uso] = amb.api.feitas("POST", "/uso")
    assert uso.corpo["tokens_entrada"] == 500
    assert eventos[3][1]["uso"]["tokens"] == 501


# ---------------------------------------------------------------- 6. limite de passos

async def test_limite_de_passos_avisa_em_vez_de_terminar_calado(ambiente):
    amb = ambiente([Resposta([ferramenta("listar_obras", {})], stop_reason="tool_use")])
    amb.dep.config = replace(amb.dep.config, max_iteracoes=1)
    eventos = await amb.conversar("quais obras?")

    assert ("erro", {"mensagem": AVISO_LIMITE_PASSOS}) in eventos
    assert [e for e, _ in eventos][-1] == "fim"
    assert len(amb.anthropic.pedidos) == 1


# ---------------------------------------------------------------- 7. contexto que só cresce

async def test_resultados_antigos_de_ferramenta_sao_limpos_no_servidor(ambiente):
    amb = ambiente([Resposta([texto("Oi!")])])
    await amb.conversar("oi")
    [p] = amb.anthropic.pedidos
    assert "context-management-2025-06-27" in amb.anthropic.cabecalhos[0]["anthropic-beta"]
    [edicao] = p["context_management"]["edits"]
    assert edicao["type"] == "clear_tool_uses_20250919"
    assert edicao["trigger"] == {"type": "input_tokens", "value": 150_000}
    assert edicao["clear_at_least"] == {"type": "input_tokens", "value": 30_000}
    # O que foi proposto nunca some da vista do modelo (senão ele propõe de novo).
    assert edicao["exclude_tools"] == ["propor_ajuste_estoque", "propor_avanco_pedido", "propor_pedido"]
