"""As ferramentas da Fase 5 contra a API dublada: o modelo recebe números prontos, nunca conta para fazer."""
import json
from datetime import date

import pytest
from anthropic.lib.tools import ToolError

from canteiro_agente.api_central import ApiCentral
from canteiro_agente.ferramentas import Eventos, criar_ferramentas

from falsos import AREIA_EMEI, BRITA_EMEI, CIMENTO_EMEI, OBRA, PED_BRITA, TOKEN_USUARIO, ApiFalsa
from falsos import OBRA_EMEI as EMEI

HOJE = date(2026, 11, 5)


@pytest.fixture
async def ferramentas():
    api_falsa = ApiFalsa()
    async with api_falsa.cliente() as http:
        fs = criar_ferramentas(ApiCentral(http, "http://api", TOKEN_USUARIO), Eventos(hoje=HOJE))
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
    assert (fund["comprometido"], fund["realizado"], fund["saldo"], fund["estourou"]) == (
        "R$ 24.250,00", "R$ 23.340,00", "R$ 91.996,82", False)
    # gasto por comprovante vem da API já somado; aqui só aparece a parte dele (e nada na etapa sem gasto)
    assert fund["gastos_por_comprovante"] == "R$ 1.500,00"
    assert "gastos_por_comprovante" not in next(e for e in o["etapas"] if e["etapa"] == "1")
    assert o["gastos_por_comprovante"] == {"total": "R$ 1.800,00", "fora_do_orcamento_etapas_extras": "R$ 300,00"}
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
    assert c["sem_previsao"] is True and "NÃO quer dizer que não precisa" in e["lembretes"][f"sem_previsao:{EMEI}"]
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
    assert api.chamadas[-2].caminho.endswith("/pedidos") and api.chamadas[-1].caminho == "/propostas"
    assert "propostas_pendentes" not in p
    vazio = await _chamar(ferramentas, "listar_pedidos", obra_id=EMEI, abertos=True, busca="cimento")
    assert vazio["pedidos"] == [] and "não existe" in vazio["aviso"]
    r = json.loads(await fs["propor_entrega"].func(
        obra_id=EMEI, pedido_id=PED_BRITA, linhas=[{"indice": 0, "quantidade": "7"}],
        justificativa="chegou o resto da brita", nf_numero="4410", nf_valor="910,00"))
    # nota sem data dita: fica hoje, e a ferramenta diz isso ao modelo
    assert r["titulo"] == "PED-0170: dar entrada de 7 m³ de Brita 1 (NF 4410, 05/11/2026, R$ 910,00)"
    assert "ficou hoje, 05/11/2026" in r["nf_data"]
    [corpo] = [c.corpo for c in api.feitas("POST", "/propostas")]
    assert (corpo["tipo"], corpo["dados"]) == ("registrar_entrega", {
        "pedido_id": PED_BRITA, "linhas": [{"indice": 0, "quantidade": 7_000}], "nf_numero": "4410",
        "nf_data": "2026-11-05", "nf_valor": 91_000})


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
    # a próxima a começar é dezembro, que já está no mês seguinte: não se repete à parte
    assert r["prazo"]["proximo_mes"]["mes"] == "dez/2026" and r["prazo"]["proxima_a_comecar"] is None
    assert r["prazo"]["medido_x_previsto"] == "4,0% medido × 3,9% previsto"
    assert r["financeiro"]["aditivos_pct"] == "2,9%" and r["aprovacoes_pendentes"]["quantas"] == 1
    assert "sem previsão" in r["lembretes"][f"sem_previsao:{EMEI}"]
    v = await _chamar(ferramentas, "resumo_obra", obra_id=OBRA)
    assert v["prazo"] == {"cadastrado": False, "termino_contratual": None}
    assert v["financeiro"] == {"tem_orcamento": False}
    assert [x.split(":")[0] for x in v["lembretes"].values()] == ["Cronograma não cadastrado",
                                                                  "Orçamento não importado"]
    assert v["entregas"]["atrasadas"] == ["PED-0158 (Gerdau): 3.200 kg de Aço CA-50, previsto para 03/11/2026"]
    assert v["almoxarifado"]["falta_comprar"][0].startswith("Cimento CP-II: precisa 200 sc")


# --- segundo teste com o modelo real (05/11/2026) ---

async def test_resumo_lista_o_mes_seguinte_e_nao_diz_que_etapa_medida_comeca(ferramentas):
    r = await _chamar(ferramentas, "resumo_obra", obra_id=EMEI)
    dez = r["prazo"]["proximo_mes"]
    assert (dez["mes"], dez["previsto"]) == ("dez/2026", "R$ 162.707,92")
    assert dez["etapas"] == [
        "3 FUNDAÇÕES: 70% no mês (R$ 74.119,60), continua (adiantada)",
        "2 MOVIMENTO DE TERRA: 85% no mês (R$ 56.652,73), continua (adiantada)",
        "23 SERVIÇOS COMPLEMENTARES: 25% no mês (R$ 17.973,12), continua (atrasada)",
        "4 SUPERESTRUTURA: 10% no mês (R$ 11.295,68), começa neste mês",
        "21 SPDA: 15% no mês (R$ 2.666,79), começa neste mês",
    ]
    assert not any("FUNDAÇÕES" in x and "começa" in x for x in json.dumps(r, ensure_ascii=False).split('"'))


async def test_lembrete_sai_uma_vez_por_conversa(ferramentas):
    r1 = await _chamar(ferramentas, "consultar_estoque", obra_id=EMEI)
    assert f"sem_previsao:{EMEI}" in r1["lembretes"]
    # a mesma falta de previsão, de novo pelo almoxarifado e pelo resumo: só a chave, como já dita
    r2 = await _chamar(ferramentas, "consultar_estoque", obra_id=EMEI, busca="brita")
    r3 = await _chamar(ferramentas, "resumo_obra", obra_id=EMEI)
    for r in (r2, r3):
        assert "lembretes" not in r
        assert r["lembretes_ja_ditos"]["chaves"] == [f"sem_previsao:{EMEI}"]
        assert "não repita" in r["lembretes_ja_ditos"]["nota"]
    # data futura (aditivo de 20/11 com hoje 05/11): uma vez também
    o1 = await _chamar(ferramentas, "consultar_orcamento", obra_id=EMEI)
    assert list(o1["lembretes"]) == [f"data_futura:aditivo:{EMEI}:1"] and "20/11/2026" in o1["lembretes"][
        f"data_futura:aditivo:{EMEI}:1"]
    o2 = await _chamar(ferramentas, "consultar_orcamento", obra_id=EMEI)
    assert "lembretes" not in o2 and o2["lembretes_ja_ditos"]["chaves"] == [f"data_futura:aditivo:{EMEI}:1"]


def test_lembretes_ja_dados_saem_do_historico_guardado():
    from canteiro_agente.ferramentas import lembretes_do_historico

    resultado = json.dumps({"insumos": [], "lembretes": {"sem_previsao:obra-emei": "x"}})
    mensagens = [
        {"role": "user", "content": [{"type": "text", "text": "quanto tem de brita?"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "consultar_estoque", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": resultado}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2",
                                      "content": [{"type": "text", "text": json.dumps({"lembretes": {"b": "y"}})}]}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t3", "content": "não é json lembretes"}]},
    ]
    assert lembretes_do_historico(mensagens) == {"sem_previsao:obra-emei", "b"}


async def test_cronograma_marca_data_futura_da_observacao_da_medicao():
    from canteiro_agente.ferramentas import _datas_futuras

    assert _datas_futuras("aço das sapatas chegou em 30/11", HOJE) == ["30/11/2026"]
    assert _datas_futuras("chegou em 03/11/2026; nada em 31/02", HOJE) == []


async def test_historico_sem_insumo_traz_todos_os_insumos_da_obra(ferramentas):
    fs, api = ferramentas
    h = await _chamar(ferramentas, "historico_movimentos", obra_id=EMEI)
    assert api.chamadas[-2].caminho == f"/obras/{EMEI}/movimentos"            # uma leitura, a obra inteira
    assert {m["insumo"] for m in h["movimentos"]} == {"Brita 1", "Cimento CP-II 50 kg", "Areia média"}
    assert h["movimentos"][0] == {
        "insumo": "Brita 1", "insumo_id": BRITA_EMEI, "tipo": "entrada", "quantidade_milesimos": 18_000,
        "quantidade": "18 m³", "destino": None, "etapa": "3", "origem_doc": "PED-0170 · NF 4398",
        "quem": "Engenheiro Responsável", "origem": "tela", "quando": "04/11/2026 10:00"}
    assert (h["mostrando"], h["cortado"]) == (5, False) and "aviso" not in h
    # limite menor que o total: diz que cortou e quais insumos ficaram fora da janela
    c = await _chamar(ferramentas, "historico_movimentos", obra_id=EMEI, limite=1)
    assert (c["mostrando"], c["cortado"]) == (1, True)
    assert set(c["insumos_fora_desta_janela"]) == {"Cimento CP-II 50 kg", "Areia média"}
    assert "mais antigos" in c["aviso"]


async def test_entrega_aceita_hoje_na_data_da_nota(ferramentas):
    fs, api = ferramentas
    r = json.loads(await fs["propor_entrega"].func(
        obra_id=EMEI, pedido_id=PED_BRITA, linhas=[{"indice": 0, "quantidade": "7"}],
        justificativa="chegou a brita hoje", nf_numero="4410", nf_data="hoje", nf_valor="910"))
    [corpo] = [c.corpo for c in api.feitas("POST", "/propostas")]
    assert corpo["dados"]["nf_data"] == "2026-11-05" and "nf_data" not in r     # dita: sem aviso de padrão
    from canteiro_agente.ferramentas import _data_iso
    assert (_data_iso("ontem", HOJE), _data_iso("Amanhã", HOJE)) == ("2026-11-04", "2026-11-06")


async def test_pedido_pelo_preco_unitario_faz_a_conta_na_calculadora(ferramentas):
    fs, api = ferramentas
    r = json.loads(await fs["propor_pedido"].func(
        obra_id=EMEI, insumo_id=BRITA_EMEI, quantidade="7", fornecedor="Pedreira Lajeado Britagem Ltda",
        preco_unitario="130,00", justificativa="repor os 7 m³", etapa="3"))
    assert r["conta_do_valor"] == "7 m³ × R$ 130,00/m³ = R$ 910,00"
    [corpo] = [c.corpo for c in api.feitas("POST", "/propostas")]
    assert corpo["dados"]["valor"] == 91_000 and "R$ 910,00" in corpo["titulo"]
    # total e unitário que não batem: pergunta antes, não escolhe
    with pytest.raises(ToolError, match="não bate"):
        await fs["propor_pedido"].func(obra_id=EMEI, insumo_id=BRITA_EMEI, quantidade="7", fornecedor="X",
                                       valor="900", preco_unitario="130", justificativa="x", etapa="3")
    with pytest.raises(ToolError, match="Falta o valor"):
        await fs["propor_pedido"].func(obra_id=EMEI, insumo_id=BRITA_EMEI, quantidade="7", fornecedor="X",
                                       justificativa="x", etapa="3")
    # milhar e decimal: 1.250,5 kg × R$ 7,50 = R$ 9.378,75
    from canteiro_agente.ferramentas import _conta_do_total
    assert _conta_do_total(1_250_500, 750, "kg").conta == "1.250,5 kg × R$ 7,50/kg = R$ 9.378,75"


async def test_pedido_traz_as_propostas_pendentes_dele(ferramentas):
    """Rodada 3: "a brita do PED-0170 veio pela metade" respondeu "me avise que eu dou entrada" com a entrada da
    NF 4410 já esperando aprovação. O pedido agora traz o que está pendente para ele."""
    fs, api = ferramentas
    await fs["propor_entrega"].func(obra_id=EMEI, pedido_id=PED_BRITA, linhas=[{"indice": 0, "quantidade": "7"}],
                                    justificativa="chegou o resto", nf_numero="4410", nf_valor="910,00")
    ps = await _chamar(ferramentas, "listar_pedidos", obra_id=EMEI, busca="brita")
    [p] = ps
    assert [x["titulo"] for x in p["propostas_pendentes"]] == [
        "PED-0170: dar entrada de 7 m³ de Brita 1 (NF 4410, 05/11/2026, R$ 910,00)"]
    assert "não ofereça propor de novo" in p["nota_pendentes"]
    # o pedido em si não muda: falta o mesmo
    assert p["linhas"][0]["falta_entregar"] == "7 m³"
    [get] = api.feitas("GET", "/propostas")
    assert dict(get.params) == {"status": "pendente", "obra_id": EMEI, "limite": "100"}
    # recusada ou aprovada sai da lista
    next(iter(api.propostas.values()))["status"] = "recusada"
    [p] = await _chamar(ferramentas, "listar_pedidos", obra_id=EMEI, busca="brita")
    assert "propostas_pendentes" not in p


async def test_compra_nova_pendente_do_mesmo_insumo_aparece_no_pedido(ferramentas):
    fs, api = ferramentas
    api.propostas["p-compra"] = {"id": "p-compra", "status": "pendente", "obra_id": EMEI, "tipo": "criar_pedido",
                                 "efeito": "compra", "titulo": "Comprar 7 m³ de Brita 1 por R$ 910,00",
                                 "dados": {"insumo_id": BRITA_EMEI}}
    api.propostas["p-outra-obra"] = {"id": "p-outra-obra", "status": "pendente", "obra_id": OBRA,
                                     "tipo": "criar_pedido", "titulo": "outra obra", "dados": {"insumo_id": BRITA_EMEI}}
    ps = await _chamar(ferramentas, "listar_pedidos", obra_id=EMEI)
    brita = next(p for p in ps if p["numero"] == "PED-0170")
    assert [(x["titulo"], x["efeito"]) for x in brita["propostas_pendentes"]] == [
        ("Comprar 7 m³ de Brita 1 por R$ 910,00", "compra")]
    assert all("propostas_pendentes" not in p for p in ps if p["numero"] != "PED-0170")


async def test_lista_de_propostas_fora_nao_derruba_os_pedidos(ferramentas, monkeypatch):
    from canteiro_agente.api_central import ErroApi

    async def falha(self, obra_id):
        raise ErroApi(403, "sem acesso")

    monkeypatch.setattr(ApiCentral, "propostas_pendentes", falha)
    [p] = await _chamar(ferramentas, "listar_pedidos", obra_id=EMEI, busca="brita")
    assert p["numero"] == "PED-0170" and "propostas_pendentes" not in p


async def test_data_futura_do_aditivo_so_onde_o_aditivo_importa(ferramentas):
    """Rodada 3: o aviso do aditivo saía no fim de respostas sobre cimento e concreto (busca no orçamento para achar a
    etapa). Agora: visão geral ou busca que achou item do aditivo."""
    chave = f"data_futura:aditivo:{EMEI}:1"
    cimento = await _chamar(ferramentas, "consultar_orcamento", obra_id=EMEI, busca="cimento")
    assert "lembretes" not in cimento and "lembretes_ja_ditos" not in cimento
    tapume = await _chamar(ferramentas, "consultar_orcamento", obra_id=EMEI, busca="tapume")    # tem item do aditivo
    assert list(tapume["lembretes"]) == [chave]
    geral = await _chamar(ferramentas, "consultar_orcamento", obra_id=EMEI)                     # uma vez por conversa
    assert "lembretes" not in geral and geral["lembretes_ja_ditos"]["chaves"] == [chave]


async def test_aviso_da_medicao_nao_sai_na_pergunta_de_outro_mes(monkeypatch):
    import falsos

    crono = json.loads(json.dumps(falsos.CRONOGRAMA_EMEI))
    crono["medicoes"][0]["observacao"] = "aço das sapatas chegou em 30/11"
    monkeypatch.setattr(falsos, "CRONOGRAMA_EMEI", crono)
    api_falsa = ApiFalsa()
    async with api_falsa.cliente() as http:
        fs = {f.name: f for f in criar_ferramentas(ApiCentral(http, "http://api", TOKEN_USUARIO), Eventos(hoje=HOJE))}
        dez = json.loads(await fs["consultar_cronograma"].call(
            {"obra_id": EMEI, "mes": "dez/2026"}))
        assert "lembretes" not in dez
        geral = json.loads(await fs["consultar_cronograma"].call({"obra_id": EMEI}))
        assert list(geral["lembretes"]) == [f"data_futura:medicao_obs:{EMEI}:1"]
