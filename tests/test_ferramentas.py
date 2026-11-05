"""As ferramentas da Fase 5 contra a API dublada: o modelo recebe números prontos, nunca conta para fazer."""
import json

import pytest
from anthropic.lib.tools import ToolError

from canteiro_agente.api_central import ApiCentral
from canteiro_agente.ferramentas import Eventos, criar_ferramentas

from falsos import AREIA_EMEI, BRITA_EMEI, CIMENTO_EMEI, OBRA, PED_BRITA, TOKEN_USUARIO, ApiFalsa
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
    assert o["lista_aditivos"] == [{"numero": 1, "descricao": "Aterro, tapume e aço", "data": "20/11/2026",
                                    "valor": "R$ 59.831,80", "itens": 5}]
    fund = next(e for e in o["etapas"] if e["etapa"] == "3")
    assert (fund["comprometido"], fund["realizado_compras"], fund["saldo"], fund["estourou"]) == (
        "R$ 24.250,00", "R$ 23.340,00", "R$ 91.996,82", False)
    assert [(x["numero"], x["versao"]) for x in o["itens_encontrados"]] == [("1.2", "contrato"), ("A1.2", "aditivo")]


async def test_cronograma_diz_atraso_contra_o_original(ferramentas):
    c = await _chamar(ferramentas, "consultar_cronograma", obra_id=EMEI)
    assert (c["termino_projetado"], c["situacao_contra_cronograma_atual"], c["situacao_contra_original"],
            c["termino_original"], c["reprogramacoes"]) == ("set/2027", "em dia", "0,5 mês de atraso", "ago/2027", 1)
    assert (c["pct_previsto_na_medicao"], c["pct_medido"]) == ("4,1%", "3,8%")


async def test_pedido_exige_etapa_quando_a_obra_tem_orcamento(ferramentas):
    fs, api = ferramentas
    with pytest.raises(ToolError, match="diga de qual etapa"):
        await fs["propor_pedido"].func(obra_id=EMEI, insumo_id=CIMENTO_EMEI, quantidade="10", fornecedor="X",
                                       valor="400", justificativa="teste")
    with pytest.raises(ToolError, match="etapa 9 não existe"):
        await fs["propor_pedido"].func(obra_id=EMEI, insumo_id=CIMENTO_EMEI, quantidade="10", fornecedor="X",
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


# --- o que o teste com o modelo real pediu (05/11/2026) ---

async def test_sem_previsao_nao_vira_nao_precisa_e_traz_o_ultimo_preco(ferramentas):
    e = await _chamar(ferramentas, "consultar_estoque", obra_id=EMEI, busca="cimento")
    [c] = e["insumos"]
    assert c["sem_previsao"] is True and "NÃO quer dizer que não precisa" in c["aviso"]
    assert c["linha_conta"].startswith("sem previsão lançada") and "= falta" not in c["linha_conta"]
    assert c["ultimo_preco"] == {"unitario": "R$ 37,50/sc", "unitario_centavos": 3750, "pedido": "PED-0169",
                                 "fornecedor": "Materiais de Construção Vale do Taquari Ltda", "data": "03/11/2026"}


def test_linha_de_conta_sem_texto_da_api_nunca_escreve_conta_falsa():
    from canteiro_agente.ferramentas import linha_de_conta

    base = {"saldo": 60_000, "a_caminho": 0, "cotando": 0, "transferencia": 0}
    assert linha_de_conta({**base, "precisa": 0, "falta": 0}, "sc").startswith("sem previsão lançada")
    assert linha_de_conta({**base, "precisa": 10_000, "falta": 0}, "sc").endswith("cobre o que precisa, falta 0")
    assert linha_de_conta({**base, "texto": "da API"}, "sc") == "da API"


async def test_calcular_mostra_a_conta_pronta(ferramentas):
    r = await _chamar(ferramentas, "calcular", expressao="10 [sc] × 37,50 [R$/sc]", unidade_resultado="R$")
    assert (r["conta"], r["resultado"]) == ("10 sc × R$ 37,50/sc = R$ 375,00", "R$ 375,00")
    with pytest.raises(ToolError, match="Divisão por zero"):
        await ferramentas[0]["calcular"].func(expressao="1 / (2 - 2)")


async def test_ajuste_pela_contagem_calcula_a_diferenca(ferramentas):
    fs, api = ferramentas
    r = json.loads(await fs["propor_ajuste_estoque"].func(obra_id=EMEI, insumo_id=AREIA_EMEI, contagem="8",
                                                          motivo="contagem física no canteiro"))
    [corpo] = [c.corpo for c in api.feitas("POST", "/propostas")]
    assert corpo["dados"]["quantidade"] == -2_000
    assert corpo["conta"] == "contagem 8 m³ − saldo no sistema 10 m³ = ajuste −2 m³"
    assert r["titulo"] == "Ajustar Areia média em −2 m³"
    with pytest.raises(ToolError, match="um dos dois"):
        await fs["propor_ajuste_estoque"].func(obra_id=EMEI, insumo_id=AREIA_EMEI, motivo="x")
    with pytest.raises(ToolError, match="bate com o saldo"):
        await fs["propor_ajuste_estoque"].func(obra_id=EMEI, insumo_id=AREIA_EMEI, contagem="10", motivo="x")


async def test_acha_o_pedido_parcial_pelo_fornecedor_e_propoe_a_entrega(ferramentas):
    fs, api = ferramentas
    ps = await _chamar(ferramentas, "listar_pedidos", obra_id=EMEI, abertos=True, busca="pedreira")
    [p] = ps
    assert (p["numero"], p["status"], p["recebe_entrega"], p["previsao"]) == ("PED-0170", "parcial", True,
                                                                              "16/11/2026")
    assert (p["linhas"][0]["indice"], p["linhas"][0]["falta_entregar"]) == (0, "7 m³")
    assert api.chamadas[-1].caminho.endswith("/pedidos")
    vazio = await _chamar(ferramentas, "listar_pedidos", obra_id=EMEI, abertos=True, busca="cimento")
    assert vazio["pedidos"] == [] and "não existe" in vazio["aviso"]
    r = json.loads(await fs["propor_entrega"].func(
        obra_id=EMEI, pedido_id=PED_BRITA, linhas=[{"indice": 0, "quantidade": "7"}],
        justificativa="chegou o resto da brita", nf_numero="4410", nf_valor="910,00"))
    assert r["titulo"] == "PED-0170: dar entrada de 7 m³ de Brita 1 (NF 4410, R$ 910,00)"
    [corpo] = [c.corpo for c in api.feitas("POST", "/propostas")]
    assert (corpo["tipo"], corpo["dados"]) == ("registrar_entrega", {
        "pedido_id": PED_BRITA, "linhas": [{"indice": 0, "quantidade": 7_000}], "nf_numero": "4410",
        "nf_valor": 91_000})


async def test_encerrar_parcial_diz_que_nao_da_entrada(ferramentas):
    r = json.loads(await ferramentas[0]["propor_avanco_pedido"].func(
        obra_id=EMEI, pedido_id=PED_BRITA, para="conferido", justificativa="a pedreira não manda o resto"))
    assert r["titulo"] == ("PED-0170: encerrar sem esperar o resto (7 m³ de Brita 1 não vêm) · sem entrada no "
                           "almoxarifado")
    assert BRITA_EMEI


async def test_cronograma_por_etapa_e_o_que_vem_no_mes(ferramentas):
    c = await _chamar(ferramentas, "consultar_cronograma", obra_id=EMEI, mes="dez/2026")
    assert c["etapas_atrasadas"] == ["1"]
    e1 = next(e for e in c["etapas"] if e["etapa"] == "1")
    assert (e1["situacao"], e1["previsto_ate_a_medicao"], e1["medido"], e1["abaixo_do_previsto"]) == (
        "atrasada", "100%", "88,86%", "11,14 pontos percentuais")
    assert e1["meses"] == {"nov/2026": {"previsto": "100% · R$ 81.529,81", "medido": "88,86%"}}
    assert [e["etapa"] for e in c["etapas"]] == ["1", "2", "4"]          # a 9 não tem nada no cronograma
    no = c["no_mes"]
    assert (no["mes"], no["previsto_no_mes"], no["pct_previsto_acumulado_no_fim_do_mes"]) == (
        "dez/2026", "R$ 162.707,92", "11,8%")
    assert [(e["etapa"], e["previsto_no_mes"], e["valor_no_mes"]) for e in no["etapas"]] == [
        ("2", "85%", "R$ 56.652,73"), ("4", "100%", "R$ 112.956,80")]
    with pytest.raises(ToolError, match="não entendido"):
        await ferramentas[0]["consultar_cronograma"].func(obra_id=EMEI, mes="natal")


async def test_resumo_da_obra_com_e_sem_cadastro(ferramentas):
    r = await _chamar(ferramentas, "resumo_obra", obra_id=EMEI)
    assert (r["obra"]["situacao"], r["hoje"]) == ("em andamento", "05/11/2026")
    assert r["prazo"]["etapas_atrasadas"] == ["1 SERVIÇOS PRELIMINARES: 88,86% medido de 100% previsto"]
    assert r["prazo"]["proximas"]["mes"] == "dez/2026"
    assert r["prazo"]["medido_x_previsto"] == "4,0% medido × 3,9% previsto"
    assert r["financeiro"]["aditivos_pct"] == "2,9%" and r["aprovacoes_pendentes"]["quantas"] == 1
    assert any("sem previsão" in x for x in r["falta_cadastrar"])
    v = await _chamar(ferramentas, "resumo_obra", obra_id=OBRA)
    assert v["prazo"] == {"cadastrado": False, "termino_contratual": None}
    assert v["financeiro"] == {"tem_orcamento": False}
    assert [x.split(":")[0] for x in v["falta_cadastrar"]] == ["Cronograma não cadastrado", "Orçamento não importado"]
    assert v["entregas"]["atrasadas"] == ["PED-0158 (Gerdau): 3.200 kg de Aço CA-50, previsto para 03/11/2026"]
    assert v["almoxarifado"]["falta_comprar"][0].startswith("Cimento CP-II: precisa 200 sc")
