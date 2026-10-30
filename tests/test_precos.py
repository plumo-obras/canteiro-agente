from types import SimpleNamespace as NS

import pytest

from canteiro_agente.precos import ModeloSemPreco, Tokens, custo_microdolares, medir


def test_custo_em_microdolares_inteiros_arredondado_uma_vez():
    # Haiku 5.5: 0,10 entrada e 0,01 leitura de cache por milhão → 3 tokens de entrada = 0,3 µUS$; com 7 leituras,
    # 0,37 → arredonda para 0 só no fim (não 0 + 0 por parcela).
    assert custo_microdolares("claude-haiku-5-5", Tokens(entrada=3, cache_leitura=7)) == 0
    assert custo_microdolares("claude-haiku-5-5", Tokens(entrada=5, cache_leitura=10)) == 1   # 0,5 + 0,1 = 0,6
    assert custo_microdolares("claude-opus-5-5", Tokens(entrada=1_000_000)) == 4_000_000      # US$ 4,00


def test_modelo_sem_preco_falha_alto():
    with pytest.raises(ModeloSemPreco):
        custo_microdolares("claude-inexistente", Tokens(entrada=1))


def test_sem_fallback_usa_o_usage_do_topo_no_preco_do_modelo_que_respondeu():
    usage = NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=0,
               iterations=None)
    tokens, custo = medir("claude-opus-5-5", "claude-opus-5-5", usage)
    assert tokens == Tokens(1000, 100, 0, 0)
    assert custo == 1000 * 4 + 100 * 20


def test_com_fallback_cada_modelo_no_seu_preco():
    it = lambda tipo, modelo, e, s: NS(type=tipo, model=modelo, input_tokens=e, output_tokens=s,  # noqa: E731
                                       cache_read_input_tokens=0, cache_creation_input_tokens=0)
    usage = NS(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0,
               iterations=[it("message", None, 1000, 10), it("fallback_message", "claude-opus-4-8", 1000, 300)])
    tokens, custo = medir("claude-opus-5-5", "claude-opus-4-8", usage)
    assert tokens == Tokens(2000, 310, 0, 0)
    assert custo == (1000 * 4 + 10 * 20) + (1000 * 5 + 300 * 25)
