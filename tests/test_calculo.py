"""A calculadora do agente: o modelo escreve a conta, o código avalia (Decimal, sem eval)."""
from decimal import Decimal

import pytest

from canteiro_agente.calculo import ErroConta, calcular


@pytest.mark.parametrize("expressao,unidade,conta", [
    ("7 [m³] × 130,00 [R$/m³]", "R$", "7 m³ × R$ 130,00/m³ = R$ 910,00"),
    ("10 [sc] * 37,5 [R$/sc]", "R$", "10 sc × R$ 37,50/sc = R$ 375,00"),
    ("8 [m³] − 10 [m³]", "m³", "8 m³ − 10 m³ = −2 m³"),
    ("2 + 3 × 4", None, "2 + 3 × 4 = 14"),                         # precedência: × antes de +
    ("(2 + 3) × 4", None, "(2 + 3) × 4 = 20"),
    ("10 − 4 − 3", None, "10 − 4 − 3 = 3"),                        # associa à esquerda
    ("12 ÷ 4 ÷ 3", None, "12 ÷ 4 ÷ 3 = 1"),
    ("-2 × -3", None, "−2 × −3 = 6"),
    ("2094833,38 [R$] − 82872,85 [R$]", "R$", "R$ 2.094.833,38 − R$ 82.872,85 = R$ 2.011.960,53"),
    ("1.299,90 [R$] x 2", "R$", "R$ 1.299,90 × 2 = R$ 2.599,80"),
])
def test_contas(expressao, unidade, conta):
    assert calcular(expressao, unidade).conta == conta


def test_arredonda_meio_para_cima_no_centavo_e_no_milesimo():
    assert calcular("1 / 3", "R$").valor == Decimal("0.33")
    assert calcular("0,125 × 1", "R$").resultado == "R$ 0,13"          # meio centavo sobe (não é o do banqueiro)
    assert calcular("0,0025 × 1", "R$").resultado == "R$ 0,00"
    assert calcular("2 / 3", "m³").resultado == "0,667 m³"
    assert calcular("0,1 + 0,2", None).valor == Decimal("0.300")      # Decimal, não float


@pytest.mark.parametrize("ruim,mensagem", [
    ("1 / 0", "Divisão por zero"),
    ("5 ÷ (2 − 2)", "Divisão por zero"),
    ("2 ** 99999", "mal escrita"),
    ("2 ^ 99999", "não entendi"),
    ("1e999", "não entendi"),
    ("abs(-2)", "não entendi"),
    ("__import__('os').system('ls')", "não entendi"),
    ("x", "não tem número"),
    ("preco × 2", "não entendi"),
    ("(1 + 2", "mal escrita"),
    ("2.760 × 1", "ambíguo"),
    ("[m³] 2", "logo depois de um número"),
    ("", "vazia"),
    ("9" * 20 + " × " + "9" * 20, "grande demais"),
    ("1 + " * 100 + "1", "longa demais"),
])
def test_recusa(ruim, mensagem):
    with pytest.raises(ErroConta, match=mensagem):
        calcular(ruim)


def test_numeros_demais():
    with pytest.raises(ErroConta, match="Números demais"):
        calcular(" + ".join(["1"] * 31))
