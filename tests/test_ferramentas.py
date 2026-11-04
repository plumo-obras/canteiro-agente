"""As ferramentas da Fase 5 contra a API dublada: o modelo recebe números prontos, nunca conta para fazer."""
import json

import pytest
from anthropic.lib.tools import ToolError

from canteiro_agente.api_central import ApiCentral
from canteiro_agente.ferramentas import Eventos, criar_ferramentas

from falsos import CIMENTO, OBRA, TOKEN_USUARIO, ApiFalsa
from falsos import OBRA_EMEI as EMEI


@pytest.fixture
async def ferramentas():
    api_falsa = ApiFalsa()
    async with api_falsa.cliente() as http:
        fs = criar_ferramentas(ApiCentral(http, "http://api", TOKEN_USUARIO), Eventos())
        yield {f.name: f for f in fs}, api_falsa


async def _chamar(fs, nome, **entrada):
    return json.loads(await fs[0][nome].call(entrada))


async def test_orcamento_traz_aditivo_e_andamento_prontos(ferramentas):
    o = await _chamar(ferramentas, "consultar_orcamento", obra_id=EMEI, busca="tapume")
    assert (o["contratado"], o["aditivos"], o["aditivos_pct_sobre_contrato"], o["orcado_atual"]) == (
        "R$ 2.035.001,58", "R$ 59.831,80", "2,9%", "R$ 2.094.833,38")
    assert o["lista_aditivos"] == [{"numero": 1, "descricao": "Aterro, tapume e aço", "data": "2026-11-20",
                                    "valor": "R$ 59.831,80", "itens": 5}]
    fund = next(e for e in o["etapas"] if e["etapa"] == "3")
    assert (fund["comprometido"], fund["realizado_compras"], fund["saldo"], fund["estourou"]) == (
        "R$ 24.250,00", "R$ 23.340,00", "R$ 91.996,82", False)
    assert [(x["numero"], x["versao"]) for x in o["itens_encontrados"]] == [("1.2", "contrato"), ("A1.2", "aditivo")]


async def test_cronograma_diz_atraso_contra_o_original(ferramentas):
    c = await _chamar(ferramentas, "consultar_cronograma", obra_id=EMEI)
    assert (c["termino_projetado"], c["situacao_contra_cronograma_atual"], c["situacao_contra_original"],
            c["termino_original"], c["reprogramacoes"]) == ("2027-09", "em dia", "0,5 mês de atraso", "2027-08", 1)
    assert (c["pct_previsto_na_medicao"], c["pct_medido"]) == ("4,1%", "3,8%")


async def test_pedido_exige_etapa_quando_a_obra_tem_orcamento(ferramentas):
    fs, api = ferramentas
    with pytest.raises(ToolError, match="diga de qual etapa"):
        await fs["propor_pedido"].func(obra_id=EMEI, insumo_id=CIMENTO, quantidade="10", fornecedor="X",
                                       valor="400", justificativa="teste")
    with pytest.raises(ToolError, match="etapa 9 não existe"):
        await fs["propor_pedido"].func(obra_id=EMEI, insumo_id=CIMENTO, quantidade="10", fornecedor="X",
                                       valor="400", justificativa="teste", etapa="9")


async def test_fornecedores_e_obra_sem_orcamento(ferramentas):
    fs = await _chamar(ferramentas, "listar_fornecedores", busca="jorge")
    assert fs == [{"nome": "Depósito São Jorge", "cnpj": "11222333000181", "contato": "Jorge",
                   "telefone": "51 9999", "email": None}]
    obras = await _chamar(ferramentas, "listar_obras")
    assert obras[0]["valor_do_contrato"] == "R$ 480.000,00" and "verba" not in obras[0]
    assert OBRA


async def test_id_vindo_do_modelo_nao_vira_outra_rota():
    """Um id com barra, ponto ou query (injeção de instrução num dado da obra) é recusado antes de chegar à API."""
    import httpx
    import pytest

    from canteiro_agente.api_central import ApiCentral, ErroApi

    pedidos = []
    transporte = httpx.MockTransport(lambda req: pedidos.append(req) or httpx.Response(200, json=[]))
    api = ApiCentral(httpx.AsyncClient(transport=transporte), "http://api.teste", "Bearer t")
    for ruim in ["x/../../interno/chave-anthropic", "obra?status=x", "a b", "", "x" * 65]:
        with pytest.raises(ErroApi):
            await api.insumos(ruim)
    assert pedidos == []
    await api.insumos("0b6f5f0e-6a8d-4f53-9a37-2f1f6c1b9e21")
    assert pedidos[0].url.path == "/obras/0b6f5f0e-6a8d-4f53-9a37-2f1f6c1b9e21/insumos"
