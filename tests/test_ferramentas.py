"""As ferramentas da Fase 5 contra a API dublada: o modelo recebe números prontos, nunca conta para fazer."""
import json
from datetime import date

import pytest
from anthropic.lib.tools import ToolError

from canteiro_agente.api_central import ApiCentral
from canteiro_agente.ferramentas import Eventos, criar_ferramentas

from falsos import OBRA, TOKEN_USUARIO, ApiFalsa
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
    assert (o["gasto"], o["falta_gastar"]) == ("R$ 118.300,00", "R$ 1.976.533,38")
    fund = next(e for e in o["etapas"] if e["etapa"] == "3")
    # o gasto e o que falta gastar vêm da API (gasto e saldo); estourada, o que falta fica negativo
    assert (fund["gasto"], fund["falta_gastar"], fund["estourou"]) == ("R$ 118.000,00", "-R$ 1.753,18", True)
    assert not {"comprometido", "realizado", "em_cotacao", "saldo"} & set(fund)
    assert not {"comprometido", "realizado", "em_cotacao", "saldo_a_comprometer"} & set(o)
    assert o["gastos_por_comprovante"] == {"total": "R$ 118.300,00", "fora_do_orcamento_etapas_extras": "R$ 300,00"}
    assert [(x["numero"], x["versao"]) for x in o["itens_encontrados"]] == [("1.2", "contrato"), ("A1.2", "aditivo")]


async def test_cronograma_diz_atraso_contra_o_original(ferramentas):
    c = await _chamar(ferramentas, "consultar_cronograma", obra_id=EMEI)
    assert (c["termino_projetado"], c["situacao_contra_cronograma_atual"], c["situacao_contra_original"],
            c["termino_original"], c["reprogramacoes"]) == ("set/2027", "em dia", "0,5 mês de atraso", "ago/2027", 1)
    assert (c["pct_previsto_na_medicao"], c["pct_medido"]) == ("4,1%", "3,8%")


async def test_obras_trazem_o_valor_do_contrato(ferramentas):
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
            await api.orcamento(ruim)
    assert pedidos == []
    await api.orcamento("0b6f5f0e-6a8d-4f53-9a37-2f1f6c1b9e21")
    assert pedidos[0].url.path == "/obras/0b6f5f0e-6a8d-4f53-9a37-2f1f6c1b9e21/orcamento"


# --- o que o teste com o modelo real pediu (05/11/2026) ---

async def test_calcular_mostra_a_conta_pronta(ferramentas):
    r = await _chamar(ferramentas, "calcular", expressao="10 [sc] × 37,50 [R$/sc]", unidade_resultado="R$")
    assert (r["conta"], r["resultado"]) == ("10 sc × R$ 37,50/sc = R$ 375,00", "R$ 375,00")
    with pytest.raises(ToolError, match="Divisão por zero"):
        await ferramentas[0]["calcular"].func(expressao="1 / (2 - 2)")


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
    assert (r["financeiro"]["gasto"], r["financeiro"]["falta_gastar"]) == ("R$ 118.300,00", "R$ 1.976.533,38")
    assert r["financeiro"]["etapas_estouradas"] == ["3 FUNDAÇÕES (passou R$ 1.753,18 do orçado)"]
    # sem compra no produto: nada de entregas nem almoxarifado no panorama
    assert "entregas" not in r and "almoxarifado" not in r
    v = await _chamar(ferramentas, "resumo_obra", obra_id=OBRA)
    assert v["prazo"] == {"cadastrado": False, "termino_contratual": None}
    assert v["financeiro"] == {"tem_orcamento": False}
    assert [x.split(":")[0] for x in v["lembretes"].values()] == ["Cronograma não cadastrado",
                                                                  "Orçamento não importado"]


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
    r1 = await _chamar(ferramentas, "resumo_obra", obra_id=OBRA)
    chaves = [f"sem_cronograma:{OBRA}", f"sem_orcamento:{OBRA}"]
    assert list(r1["lembretes"]) == chaves
    # o mesmo cadastro faltando, de novo pelo resumo: só a chave, como já dita
    r2 = await _chamar(ferramentas, "resumo_obra", obra_id=OBRA)
    assert "lembretes" not in r2
    assert r2["lembretes_ja_ditos"]["chaves"] == chaves
    assert "não repita" in r2["lembretes_ja_ditos"]["nota"]
    # data futura (aditivo de 20/11 com hoje 05/11) achada pela busca: uma vez também
    o1 = await _chamar(ferramentas, "consultar_orcamento", obra_id=EMEI, busca="tapume")
    assert list(o1["lembretes"]) == [f"data_futura:aditivo:{EMEI}:1"] and "20/11/2026" in o1["lembretes"][
        f"data_futura:aditivo:{EMEI}:1"]
    o2 = await _chamar(ferramentas, "consultar_orcamento", obra_id=EMEI, busca="tapume")
    assert "lembretes" not in o2 and o2["lembretes_ja_ditos"]["chaves"] == [f"data_futura:aditivo:{EMEI}:1"]


def test_lembretes_ja_dados_saem_do_historico_guardado():
    from canteiro_agente.ferramentas import lembretes_do_historico

    resultado = json.dumps({"obra": {}, "lembretes": {"sem_cronograma:obra-emei": "x"}})
    mensagens = [
        {"role": "user", "content": [{"type": "text", "text": "como tá a obra?"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "resumo_obra", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": resultado}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2",
                                      "content": [{"type": "text", "text": json.dumps({"lembretes": {"b": "y"}})}]}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t3", "content": "não é json lembretes"}]},
    ]
    assert lembretes_do_historico(mensagens) == {"sem_cronograma:obra-emei", "b"}


async def test_cronograma_marca_data_futura_da_observacao_da_medicao():
    from canteiro_agente.ferramentas import _datas_futuras

    assert _datas_futuras("aço das sapatas chegou em 30/11", HOJE) == ["30/11/2026"]
    assert _datas_futuras("chegou em 03/11/2026; nada em 31/02", HOJE) == []


async def test_data_futura_do_aditivo_so_onde_o_aditivo_importa(ferramentas):
    """Rodada 3: o aviso do aditivo saía no fim de respostas sobre cimento e concreto (busca no orçamento para achar a
    etapa). Rodada 7: saía também em "gasto por etapa" (visão geral). Agora o lembrete só vem na busca que achou item
    do aditivo; na visão geral a data fica marcada no próprio aditivo."""
    chave = f"data_futura:aditivo:{EMEI}:1"
    geral = await _chamar(ferramentas, "consultar_orcamento", obra_id=EMEI)
    assert "lembretes" not in geral and "lembretes_ja_ditos" not in geral
    assert "avise só se" in geral["lista_aditivos"][0]["data_depois_de_hoje"]
    cimento = await _chamar(ferramentas, "consultar_orcamento", obra_id=EMEI, busca="cimento")
    assert "lembretes" not in cimento and "lembretes_ja_ditos" not in cimento
    tapume = await _chamar(ferramentas, "consultar_orcamento", obra_id=EMEI, busca="tapume")    # tem item do aditivo
    assert list(tapume["lembretes"]) == [chave] and "data_depois_de_hoje" not in tapume["lista_aditivos"][0]
    de_novo = await _chamar(ferramentas, "consultar_orcamento", obra_id=EMEI, busca="tapume")   # uma vez por conversa
    assert "lembretes" not in de_novo and de_novo["lembretes_ja_ditos"]["chaves"] == [chave]


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


async def test_tributos_traz_as_linhas_e_a_economia_com_nota(ferramentas):
    t = await _chamar(ferramentas, "consultar_tributos", obra_id=OBRA)
    assert (t["cno"], t["mes_do_vau"]) == ("90.012.34567/89", "out/2026")
    assert (t["vau"], t["origem_do_vau"]) == ("R$ 2.000,00/m²", "tabela VAU oficial do SERO (Receita)")
    i = t["inss"]
    assert (i["situacao"], i["valor_estimado"], i["valor_estimado_centavos"]) == ("calculado", "R$ 5.246,52",
                                                                                  524_652)
    assert (i["fator_social"], i["base"], i["economia_se_tiver_nota"]) == ("40%", "R$ 14.256,85", "R$ 1.472,00")
    assert i["linhas_conta"][6] == "INSS = R$ 14.256,85 × 36,8% = R$ 5.246,52"
    assert (i["fator_de_ajuste"], i["falta_declarar_para_o_fator_de_ajuste"]) == ("não atingido", "R$ 3.480,00")
    assert t["aferida_em"] == "nov/2026"
    b = t["ibs_cbs"]
    assert b["situacao"] == "nao_incide" and "a_pagar" not in b and b["creditos_das_compras"] == "R$ 300,00"
    assert t["mao_de_obra_sem_nota"] == [{"favorecido": "Zé Empreiteiro", "descricao": "alvenaria do térreo",
                                          "valor": "R$ 10.000,00", "data": "30/10/2026"}]
    assert t["gastos_a_classificar"] == {"quantos": 1, "valor": "R$ 700,00"}
    assert "contador" in t["aviso"]


async def test_tributos_sem_dados_nao_inventa_valor(monkeypatch):
    import falsos

    t0 = falsos.TRIBUTOS_VILA
    monkeypatch.setattr(falsos, "TRIBUTOS_VILA", {
        **t0, "inss": {**t0["inss"], "situacao": "sem_dados", "linhas": [],
                       "motivos": ["Falta informar a área construída da obra."]},
        "ibs_cbs": {**t0["ibs_cbs"], "situacao": "calculado", "a_pagar": 3_625_000, "debito": 6_625_000,
                    "base": 50_000_000, "aliquota_efetiva": 1325}})
    async with ApiFalsa().cliente() as http:
        fs = {f.name: f for f in criar_ferramentas(ApiCentral(http, "http://api", TOKEN_USUARIO), Eventos(hoje=HOJE))}
        t = json.loads(await fs["consultar_tributos"].call({"obra_id": OBRA}))
    assert "valor_estimado" not in t["inss"] and t["inss"]["motivos"]
    assert (t["ibs_cbs"]["a_pagar"], t["ibs_cbs"]["aliquota_efetiva"]) == ("R$ 36.250,00", "13,25%")


async def test_propor_cronograma_usa_a_produtividade_do_orcamento(ferramentas):
    # a duração vem da quantidade × produtividade dos serviços da etapa (o modelo só dá equipe e dependências), e o
    # corpo gravado continua etapa × mês (%). O orçamento dublado só tem serviços na etapa "1".
    fs, api = ferramentas
    await fs["propor_cronograma"].func(
        obra_id=EMEI, inicio="2026-01",
        etapas=[{"etapa": "1", "equipe": 4}],
        justificativa="serviços preliminares primeiro")
    [corpo] = [c.corpo for c in api.feitas("POST", "/propostas")]
    assert corpo["tipo"] == "cronograma"
    assert corpo["dados"]["inicio"] == "2026-01"
    assert corpo["dados"]["meses"] >= 1
    etapas = corpo["dados"]["etapas"]
    assert [e["etapa"] for e in etapas] == ["1"]  # a única etapa do orçamento com serviços
    for e in etapas:
        assert sum(e["percentuais"]) == 10000
        assert len(e["percentuais"]) == corpo["dados"]["meses"]
    assert "produtividade" in corpo["fonte"]  # ancorado em obra, não em preço

    with pytest.raises(ToolError, match="Início inválido"):
        await fs["propor_cronograma"].func(obra_id=EMEI, inicio="xx",
                                           etapas=[{"etapa": "1", "equipe": 4}], justificativa="x")
    with pytest.raises(ToolError):
        await fs["propor_cronograma"].func(obra_id=EMEI, inicio="2026-01", etapas=[], justificativa="x")
