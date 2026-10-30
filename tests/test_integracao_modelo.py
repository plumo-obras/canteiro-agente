"""Integração com o modelo de verdade: GASTA TOKENS. Pulado por padrão.

Rode só de propósito: RODAR_COM_MODELO=1 ANTHROPIC_API_KEY=… .venv/bin/pytest -m modelo
A API central continua falsa; só o modelo é real.
"""
import os

import pytest
from anthropic import AsyncAnthropic

from falsos import OBRA

pytestmark = [
    pytest.mark.modelo,
    pytest.mark.skipif(os.environ.get("RODAR_COM_MODELO") != "1", reason="gasta tokens: defina RODAR_COM_MODELO=1"),
]


async def test_pergunta_de_falta_usa_a_ferramenta_e_cita_a_conta(ambiente):
    amb = ambiente([])
    amb.dep.cliente_plataforma = AsyncAnthropic()
    eventos = await amb.conversar("Quanto falta comprar de cimento?", obra_id=OBRA)
    nomes = [e for e, _ in eventos]
    assert "erro" not in nomes, eventos
    assert any(e == "ferramenta" and d["nome"] == "consultar_estoque" for e, d in eventos)
    resposta = "".join(d["delta"] for e, d in eventos if e == "texto")
    assert "34" in resposta                              # a falta que a API calculou, não uma conta do modelo
    assert amb.api.feitas("POST", "/uso")
