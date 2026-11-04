"""Achados do teste de ponta a ponta: API fora do ar, sessão alheia, duas voltas na mesma conversa, proposta em
dobro, conta feita no agente e o evento `ferramenta` que só saía no fim."""
import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from canteiro_agente.api_central import FORA_DO_AR, ApiCentral, ApiForaDoAr
from canteiro_agente.conversa import (
    AVISO_DIVERGIU,
    AVISO_OCUPADA,
    PedidoConversa,
    aguardar_pendentes,
    conversar,
)
from canteiro_agente.ferramentas import Eventos, criar_ferramentas
from canteiro_agente.main import criar_app
from canteiro_agente.sessoes import HistoricoDivergiu, NaoEncontrada, SessoesApi

import falsos
from falsos import ACO, BLOCO, OBRA, OBRA_EMEI, TOKEN_USUARIO, ApiFalsa, Resposta, ferramenta, pausada, texto

PEDIDO_CIMENTO = {"obra_id": OBRA, "insumo_id": "insumo-cimento", "quantidade": "34", "fornecedor": "Votorantim",
                  "valor": "1.360,00", "justificativa": "repor a falta de cimento"}


def _resultados(pedido_anthropic):
    return [b for b in pedido_anthropic["messages"][-1]["content"] if b["type"] == "tool_result"]


def _conteudo(bloco) -> str:
    c = bloco["content"]
    return c if isinstance(c, str) else "".join(p.get("text", "") for p in c)


class _Interceptada(httpx.AsyncBaseTransport):
    """A API falsa, com uma rota trocada por outra resposta."""

    def __init__(self, api: ApiFalsa, metodo: str, caminho: str, resposta: httpx.Response):
        self.api, self.alvo, self.resposta = api, (metodo, caminho), resposta

    async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
        if (req.method, req.url.path) == self.alvo:
            return self.resposta
        return self.api(req)


# ---------------------------------------------------------------- 2. API fora do ar

async def test_erro_de_rede_vira_api_fora_do_ar_sem_texto_cru():
    def recusar(req):
        raise httpx.ConnectError("All connection attempts failed", request=req)

    for transporte in (httpx.MockTransport(recusar), httpx.MockTransport(lambda r: httpx.Response(503, text="x"))):
        api = ApiCentral(httpx.AsyncClient(transport=transporte), "http://api.teste", TOKEN_USUARIO)
        with pytest.raises(ApiForaDoAr) as e:
            await api.obras()
        assert api.fora_do_ar and e.value.mensagem == FORA_DO_AR and e.value.status == 503


async def test_api_caida_no_meio_para_a_volta_sem_chamar_o_modelo_de_novo(ambiente):
    amb = ambiente([
        Resposta([texto("Vou ver o estoque."), ferramenta("consultar_estoque", {"obra_id": OBRA})],
                 stop_reason="tool_use", entrada=900),
        Resposta([texto("não devia chegar aqui")]),
    ])
    amb.api.fora_do_ar_depois = 0
    eventos = await amb.conversar("quanto tem de aço?", obra_id=OBRA)

    assert ("erro", {"mensagem": FORA_DO_AR}) in eventos
    assert [e for e, _ in eventos][-1] == "fim"
    assert [d["mensagem"] for e, d in eventos if e == "erro"] == [FORA_DO_AR]      # um aviso só
    assert len(amb.anthropic.pedidos) == 1                                          # sem a 2ª chamada paga
    [uso] = amb.api.feitas("POST", "/uso")                                          # o que já foi gasto, gravado
    assert uso.corpo["tokens_entrada"] == 900
    # O histórico fica válido (chamada + resultado) e o resultado não traz a exceção crua do httpx.
    [sessao] = amb.dep.sessoes._dados.values()
    resultado = sessao.mensagens[-1]["content"][0]
    assert resultado["type"] == "tool_result" and resultado.get("is_error") is True
    assert "fora do ar" in _conteudo(resultado) and "ConnectError" not in json.dumps(sessao.mensagens)


async def test_api_fora_do_ar_ao_abrir_a_conversa(ambiente):
    amb = ambiente([Resposta([texto("não devia chegar aqui")])])

    def recusar(req):
        raise httpx.ConnectError("All connection attempts failed", request=req)

    amb.dep.sessoes = SessoesApi(httpx.AsyncClient(transport=httpx.MockTransport(recusar)), "http://api.teste")
    eventos = await amb.conversar("oi")
    assert eventos == [("erro", {"mensagem": FORA_DO_AR})] and amb.anthropic.pedidos == []


# ---------------------------------------------------------------- 1. abrir a sessão: mensagem da API, sem traceback

async def test_404_da_api_passa_a_mensagem_dela():
    transporte = httpx.MockTransport(lambda r: httpx.Response(404, json={"detail": "Obra não encontrada."}))
    repo = SessoesApi(httpx.AsyncClient(transport=transporte), "http://api.teste")
    with pytest.raises(NaoEncontrada, match="^Obra não encontrada.$"):
        await repo.abrir(None, dono="u1", autorizacao=TOKEN_USUARIO, obra_id="obra-de-outro")


@pytest.mark.parametrize(("status", "corpo", "mensagem"), [
    (404, {"detail": "Obra não encontrada."}, "Obra não encontrada."),
    (404, {"detail": "Conversa não encontrada."}, "Conversa não encontrada."),
    (409, {"detail": "Já existe uma conversa com este ID."},
     "Não consegui abrir o histórico desta conversa. Tente de novo em instantes."),
    (401, {"detail": "token vencido"}, "Sua sessão expirou. Entre de novo."),
])
async def test_falha_ao_abrir_a_sessao_vira_erro_amigavel(ambiente, caplog, status, corpo, mensagem):
    amb = ambiente([Resposta([texto("não devia chegar aqui")])])
    transporte = _Interceptada(amb.api, "POST", "/sessoes", httpx.Response(status, json=corpo))
    amb.dep.sessoes = SessoesApi(httpx.AsyncClient(transport=transporte), "http://api.teste")
    eventos = await amb.conversar("oi", obra_id=OBRA)
    assert eventos == [("erro", {"mensagem": mensagem})]
    assert amb.anthropic.pedidos == []
    assert not [r for r in caplog.records if r.exc_info]          # nada de traceback no log


# ---------------------------------------------------------------- 3. duas voltas na mesma conversa

async def test_segunda_volta_na_mesma_sessao_e_recusada_antes_do_modelo(ambiente):
    amb = ambiente([
        Resposta([texto("Primeira.")]),
        pausada(Resposta([texto("Segunda, demorada.")]), 0.3),
        Resposta([texto("Terceira.")]),
    ])
    sessao_id = (await amb.conversar("oi"))[-1][1]["sessao_id"]

    comecou = asyncio.Event()
    da_lenta: list[str] = []

    async def lenta():
        async for nome, _ in conversar(amb.dep, TOKEN_USUARIO, PedidoConversa("e agora?", sessao_id=sessao_id)):
            da_lenta.append(nome)
            if nome == "texto":
                comecou.set()

    tarefa = asyncio.create_task(lenta())
    await asyncio.wait_for(comecou.wait(), 5)
    eventos = await amb.conversar("outra aba", sessao_id=sessao_id)
    assert eventos == [("erro", {"mensagem": AVISO_OCUPADA})]
    assert len(amb.anthropic.pedidos) == 2                       # a recusada não chamou o modelo
    await asyncio.wait_for(tarefa, 5)
    assert da_lenta[-1] == "fim"

    # terminada a primeira, a conversa volta a aceitar perguntas, e o histórico ficou inteiro e em ordem
    assert [e for e, _ in await amb.conversar("e o aço?", sessao_id=sessao_id)][-1] == "fim"
    guardada = amb.dep.sessoes._dados[sessao_id]
    assert [m["role"] for m in guardada.mensagens] == ["user", "assistant"] * 3


async def test_sessao_ocupada_nao_vaza_para_quem_nao_e_dono(ambiente):
    """Quem chuta o ID de uma conversa alheia em andamento ouve "não encontrada", não "ainda respondendo"."""
    amb = ambiente([Resposta([texto("Primeira.")]), pausada(Resposta([texto("Demorada.")]), 0.3)])
    sessao_id = (await amb.conversar("oi"))[-1][1]["sessao_id"]
    comecou = asyncio.Event()

    async def lenta():
        async for nome, _ in conversar(amb.dep, TOKEN_USUARIO, PedidoConversa("e agora?", sessao_id=sessao_id)):
            if nome == "texto":
                comecou.set()

    tarefa = asyncio.create_task(lenta())
    await asyncio.wait_for(comecou.wait(), 5)
    eventos = await amb.conversar("oi", sessao_id=sessao_id, autorizacao=falsos.TOKEN_OUTRO)
    assert eventos == [("erro", {"mensagem": "Conversa não encontrada."})]
    await asyncio.wait_for(tarefa, 5)
    await aguardar_pendentes()


async def test_gravacao_recusada_por_outra_instancia_avisa_a_pessoa(ambiente, monkeypatch):
    amb = ambiente([Resposta([texto("Resposta.")])])

    async def divergiu(*a, **kw):
        raise HistoricoDivergiu("seq_inicial esperado: 4")

    monkeypatch.setattr(amb.dep.sessoes, "acrescentar", divergiu)
    eventos = await amb.conversar("oi")
    assert eventos[-2:] == [("erro", {"mensagem": AVISO_DIVERGIU}), eventos[-1]] and eventos[-1][0] == "fim"


# ---------------------------------------------------------------- 4. proposta em dobro

async def test_mesma_chamada_repetida_leva_a_mesma_chave_e_nao_duplica():
    api_falsa = ApiFalsa()
    async with api_falsa.cliente() as http:
        eventos = Eventos(sessao_id="sessao-1")
        fs = {f.name: f for f in criar_ferramentas(ApiCentral(http, "http://api.teste", TOKEN_USUARIO), eventos)}
        entrada = dict(PEDIDO_CIMENTO)
        bloco = SimpleNamespace(type="tool_use", name="propor_pedido", id="toolu_9", input=entrada)
        resultados = []
        for _ in range(2):                       # a mesma chamada de ferramenta executada duas vezes
            eventos.nova_resposta([bloco])
            resultados.append(json.loads(await fs["propor_pedido"].call(entrada)))
    chaves = [c.corpo["chave_idempotencia"] for c in api_falsa.feitas("POST", "/propostas")]
    assert chaves == ["sessao-1:toolu_9"] * 2
    assert len(api_falsa.propostas) == 1
    assert [r["ja_existia"] for r in resultados] == [False, True]
    assert resultados[0]["proposta_id"] == resultados[1]["proposta_id"]
    assert "Já havia uma proposta igual" in resultados[1]["aviso"]


async def test_duas_chamadas_da_mesma_ferramenta_na_mesma_resposta_tem_chaves_proprias(ambiente):
    a = {"obra_id": OBRA, "insumo_id": BLOCO, "quantidade": "-190", "motivo": "quebra"}
    b = {"obra_id": OBRA, "insumo_id": ACO, "quantidade": "35", "motivo": "sobra achada"}
    amb = ambiente([
        Resposta([ferramenta("propor_ajuste_estoque", a, "toolu_a"), ferramenta("propor_ajuste_estoque", b, "toolu_b")],
                 stop_reason="tool_use"),
        Resposta([texto("Duas propostas aguardando aprovação.")]),
    ])
    eventos = await amb.conversar("ajusta o bloco e o aço", obra_id=OBRA)
    sessao_id = eventos[-1][1]["sessao_id"]
    posts = amb.api.feitas("POST", "/propostas")
    assert [(p.corpo["dados"]["insumo_id"], p.corpo["chave_idempotencia"]) for p in posts] == [
        (BLOCO, f"{sessao_id}:toolu_a"), (ACO, f"{sessao_id}:toolu_b")]


async def test_proposta_igual_em_outra_volta_avisa_que_ja_espera_aprovacao(ambiente):
    amb = ambiente([
        Resposta([ferramenta("propor_pedido", PEDIDO_CIMENTO, "toolu_1")], stop_reason="tool_use"),
        Resposta([texto("Proposta criada.")]),
        Resposta([ferramenta("propor_pedido", PEDIDO_CIMENTO, "toolu_2")], stop_reason="tool_use"),
        Resposta([texto("Ela já está aguardando aprovação.")]),
    ])
    sessao_id = (await amb.conversar("compra 34 sc de cimento"))[-1][1]["sessao_id"]
    eventos = await amb.conversar("compra o cimento", sessao_id=sessao_id)

    assert len(amb.api.propostas) == 1
    [proposta] = [d for e, d in eventos if e == "proposta"]
    assert proposta["ja_existia"] is True                       # o cartão é o da que já existe
    [res] = _resultados(amb.anthropic.pedidos[-1])
    corpo = json.loads(_conteudo(res))
    assert corpo["ja_existia"] is True and "Já havia uma proposta igual" in corpo["aviso"]


# ---------------------------------------------------------------- 5. a conta vem da API

async def test_orcamento_cita_saldo_estouro_e_percentual_da_api(monkeypatch):
    """Números de propósito diferentes da subtração/divisão: se o agente fizesse conta, o teste veria."""
    orc = copy.deepcopy(falsos.ORCAMENTO_EMEI)
    orc["totais"] |= {"saldo": 123, "aditivos_pct": 1234}
    orc["etapas"][1] |= {"saldo": -4500, "estourou": True}
    monkeypatch.setattr(falsos, "ORCAMENTO_EMEI", orc)
    api_falsa = ApiFalsa()
    async with api_falsa.cliente() as http:
        fs = {f.name: f for f in criar_ferramentas(ApiCentral(http, "http://api", TOKEN_USUARIO), Eventos())}
        o = json.loads(await fs["consultar_orcamento"].call({"obra_id": OBRA_EMEI}))
    assert (o["saldo_a_comprometer"], o["aditivos_pct_sobre_contrato"]) == ("R$ 1,23", "12,3%")
    fund = next(e for e in o["etapas"] if e["etapa"] == "3")
    assert (fund["saldo"], fund["estourou"]) == ("-R$ 45,00", True)


async def test_pedido_cita_o_falta_entregar_da_api(monkeypatch):
    original = ApiFalsa._pedidos

    def pedidos(self):
        ps = original(self)
        ps[0]["linhas"][0] |= {"recebida": 1_000_000, "falta_entregar": 0}     # encerrado: a API diz 0
        return ps

    monkeypatch.setattr(ApiFalsa, "_pedidos", pedidos)
    api_falsa = ApiFalsa()
    async with api_falsa.cliente() as http:
        fs = {f.name: f for f in criar_ferramentas(ApiCentral(http, "http://api", TOKEN_USUARIO), Eventos())}
        ps = json.loads(await fs["listar_pedidos"].call({"obra_id": OBRA}))
    assert (ps[0]["linhas"][0]["falta_entregar"], ps[0]["linhas"][0]["falta_entregar_milesimos"]) == ("0 kg", 0)
    assert ps[1]["linhas"][0]["falta_entregar"] == "120 sc"


# ---------------------------------------------------------------- 6. evento `ferramenta` no começo; 422 com detail

async def test_evento_ferramenta_sai_quando_a_ferramenta_comeca(ambiente):
    amb = ambiente([
        Resposta([ferramenta("consultar_estoque", {"obra_id": OBRA})], stop_reason="tool_use"),
        Resposta([texto("Pronto.")]),
    ])
    liberar = asyncio.Event()

    class Lenta(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            if req.url.path.endswith("/insumos"):
                await liberar.wait()             # a consulta só termina depois que a tela viu o anúncio
            return amb.api(req)

    amb.dep.http_api = httpx.AsyncClient(transport=Lenta())
    nomes = []

    async def ler():
        async for nome, _ in conversar(amb.dep, TOKEN_USUARIO, PedidoConversa("estoque", obra_id=OBRA)):
            nomes.append(nome)
            if nome == "ferramenta":
                liberar.set()

    await asyncio.wait_for(ler(), 3)
    assert nomes.count("ferramenta") == 1 and nomes[-1] == "fim"


@pytest.mark.parametrize(("corpo", "detail"), [
    ({"texto": ""}, "A pergunta tem de ter de 1 a 8.000 caracteres."),
    ({"texto": "a" * 8001}, "A pergunta tem de ter de 1 a 8.000 caracteres."),
    ({"texto": "oi", "obra_id": "x" * 65}, "Identificador de obra inválido."),
    ({}, "A pergunta tem de ter de 1 a 8.000 caracteres."),
])
async def test_422_do_agente_tem_detail_em_frase(ambiente, corpo, detail):
    app = criar_app(ambiente([]).dep)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agente") as c:
        r = await c.post("/conversa", json=corpo, headers={"Authorization": TOKEN_USUARIO})
    assert (r.status_code, r.json()) == (422, {"detail": detail})
