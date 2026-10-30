"""POST /conversa de ponta a ponta, com a API central e a Anthropic dubladas."""
import json

from falsos import ACO, OBRA, PED_ACO, TOKEN_SERVICO, TOKEN_USUARIO, Resposta, ferramenta, texto

FERRAMENTAS = {
    "listar_obras", "consultar_estoque", "historico_movimentos", "listar_pedidos",
    "propor_pedido", "propor_avanco_pedido", "propor_ajuste_estoque",
}


def _tool_results(pedido_anthropic):
    """Blocos tool_result da última mensagem de usuário enviada ao modelo."""
    ultima = pedido_anthropic["messages"][-1]
    assert ultima["role"] == "user"
    return [b for b in ultima["content"] if b["type"] == "tool_result"]


def _texto_resultado(bloco):
    c = bloco["content"]
    return c if isinstance(c, str) else "".join(p.get("text", "") for p in c)


async def test_consulta_chama_a_ferramenta_certa_com_o_token_do_usuario(ambiente):
    amb = ambiente([
        Resposta([texto("Vou olhar o estoque."), ferramenta("consultar_estoque", {"obra_id": OBRA, "busca": "aco"})],
                 stop_reason="tool_use"),
        Resposta([texto("Aço: precisa 3.940 kg − … = falta 0 kg. Não precisa comprar.")]),
    ])
    eventos = await amb.conversar("Quanto falta de aço?", obra_id=OBRA)

    nomes = [e for e, _ in eventos]
    assert nomes[0] == "texto" and nomes[-1] == "fim"
    ferr = [d for e, d in eventos if e == "ferramenta"]
    assert ferr == [{"nome": "consultar_estoque", "nivel": "consulta", "resumo": "consultou o almoxarifado (busca: aco)"}]
    texto_total = "".join(d["delta"] for e, d in eventos if e == "texto")
    assert "Vou olhar o estoque." in texto_total and "falta 0 kg" in texto_total

    # Toda chamada de dado foi com o token do usuário; nenhuma com o token de serviço.
    dados = [c for c in amb.api.chamadas if c.caminho != "/uso"]
    assert [c.caminho for c in dados] == ["/creditos", f"/obras/{OBRA}/insumos"]
    assert all(c.cabecalhos["authorization"] == TOKEN_USUARIO for c in dados)

    # O resultado da ferramenta voltou ao modelo com a conta bruta, formatada e a linha pronta (busca sem acento achou).
    [res] = _tool_results(amb.anthropic.pedidos[1])
    conteudo = json.loads(_texto_resultado(res))
    [aco] = conteudo["insumos"]
    assert aco["insumo_id"] == ACO
    assert aco["conta_milesimos"]["falta"] == 0
    assert aco["conta_formatada"]["precisa"] == "3.940 kg"
    assert aco["linha_conta"] == ("precisa 3.940 kg − saldo 140 kg − a caminho 3.200 kg − em cotação 600 kg"
                                  " − transferência 0 kg = falta 0 kg")


async def test_pedido_ao_modelo_segue_a_api_atual(ambiente):
    amb = ambiente([Resposta([texto("Oi!")])])
    await amb.conversar("oi")
    [p] = amb.anthropic.pedidos
    assert p["model"] == "claude-opus-5-5"
    assert p["thinking"] == {"type": "adaptive"}
    assert p["output_config"] == {"effort": "medium"}
    assert p["tool_choice"] == {"type": "auto"}
    assert p["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in amb.anthropic.cabecalhos[0]["anthropic-beta"]
    assert p["stream"] is True and "thread" not in p
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert p["cache_control"] == {"type": "ephemeral"}
    assert {t["name"] for t in p["tools"]} == FERRAMENTAS
    assert all(t.get("eager_input_streaming") is True for t in p["tools"])
    assert amb.anthropic.chaves == ["chave-plataforma"]


async def test_instrucao_e_ferramentas_sao_estaveis_entre_conversas(ambiente):
    """Cache do prefixo: ferramentas + instrução têm de ser os mesmos bytes em conversas diferentes."""
    amb = ambiente([Resposta([texto("a")]), Resposta([texto("b")])])
    await amb.conversar("um", obra_id=OBRA)
    await amb.conversar("dois")
    p1, p2 = amb.anthropic.pedidos
    assert json.dumps(p1["tools"], sort_keys=True) == json.dumps(p2["tools"], sort_keys=True)
    assert p1["system"] == p2["system"]


async def test_proposta_vira_evento_sse_e_nao_executa_nada(ambiente):
    amb = ambiente([
        Resposta([ferramenta("propor_pedido", {
            "obra_id": OBRA, "insumo_id": "insumo-cimento", "quantidade": "34", "fornecedor": "Votorantim",
            "valor": "1.360,00", "justificativa": "repor a falta de cimento"})], stop_reason="tool_use"),
        Resposta([texto("Deixei a proposta de compra de 34 sc aguardando aprovação.")]),
    ])
    eventos = await amb.conversar("Pede 34 sacos de cimento na Votorantim por 1.360")

    ferr = [d for e, d in eventos if e == "ferramenta"]
    assert ferr[0]["nome"] == "propor_pedido" and ferr[0]["nivel"] == "propoe"
    [proposta] = [d for e, d in eventos if e == "proposta"]
    assert proposta["id"] == "proposta-1" and proposta["status"] == "pendente"
    assert proposta["tipo"] == "criar_pedido"
    # A conversão para milésimos e centavos é do domínio (tipos.qtd/reais), não do modelo.
    assert proposta["dados"] == {"insumo_id": "insumo-cimento", "quantidade": 34_000, "fornecedor": "Votorantim",
                                 "valor": 136_000}
    assert proposta["conta"].endswith("= falta 34 sc")
    assert proposta["titulo"] == "Comprar 34 sc de Cimento CP-II de Votorantim por R$ 1.360,00"
    assert proposta["justificativa"] == "repor a falta de cimento"

    # Só POST /propostas: nenhuma rota que grava de verdade (pedidos, movimentos, aprovar) foi chamada.
    escritas = [c.caminho for c in amb.api.chamadas if c.metodo == "POST" and c.caminho != "/uso"]
    assert escritas == ["/propostas"]
    assert amb.api.feitas("POST", "/propostas")[0].cabecalhos["authorization"] == TOKEN_USUARIO
    # O evento proposta sai antes do texto final que fala dela.
    nomes = [e for e, _ in eventos]
    assert nomes.index("proposta") < nomes.index("texto")


async def test_avanco_invalido_e_barrado_pela_regra_do_dominio(ambiente):
    amb = ambiente([
        Resposta([ferramenta("propor_avanco_pedido", {
            "obra_id": OBRA, "pedido_id": PED_ACO, "para": "conferido", "justificativa": "chegou"})],
                 stop_reason="tool_use"),
        Resposta([texto("Esse pedido ainda está em trânsito; primeiro precisa ser recebido.")]),
    ])
    eventos = await amb.conversar("o aço chegou e já conferi")
    assert not [e for e, _ in eventos if e == "proposta"]
    assert not amb.api.feitas("POST", "/propostas")
    [res] = _tool_results(amb.anthropic.pedidos[1])
    assert res["is_error"] is True
    assert "não pode ir de 'em_transito' para 'conferido'" in _texto_resultado(res)


async def test_sem_creditos_vira_erro_e_nao_chama_o_modelo(ambiente):
    amb = ambiente([Resposta([texto("não devia")])])
    amb.api.creditos_status = 402
    eventos = await amb.conversar("oi")
    assert eventos == [("erro", {"mensagem": "Os créditos de IA do mês acabaram."})]
    assert amb.anthropic.pedidos == []
    assert not amb.api.feitas("POST", "/uso")


async def test_chave_propria_sem_chave_disponivel_nao_usa_a_da_plataforma(ambiente):
    amb = ambiente([Resposta([texto("não devia")])])
    amb.api.creditos = {"saldo_microdolares": 0, "franquia": 0, "chave_propria": True}
    eventos = await amb.conversar("oi")
    assert [e for e, _ in eventos] == ["erro"]
    assert amb.anthropic.pedidos == []


async def test_chave_propria_usa_a_chave_do_cliente_e_marca_o_uso(ambiente):
    class Fonte:
        async def chave_do_cliente(self, autorizacao):
            assert autorizacao == TOKEN_USUARIO
            return "chave-do-cliente"

    amb = ambiente([Resposta([texto("ok")])])
    amb.api.creditos = {"saldo_microdolares": 0, "franquia": 0, "chave_propria": True}
    amb.dep.fonte_chave = Fonte()
    await amb.conversar("oi")
    assert amb.anthropic.chaves == ["chave-do-cliente"]
    [uso] = amb.api.feitas("POST", "/uso")
    assert uso.corpo["chave_propria"] is True


async def test_uso_gravado_a_cada_resposta_com_o_custo_certo(ambiente):
    amb = ambiente([
        Resposta([ferramenta("listar_obras", {})], stop_reason="tool_use",
                 entrada=1000, saida=200, cache_leitura=5000, cache_escrita=3000),
        Resposta([texto("Uma obra: Casa Vila Nova.")], entrada=300, saida=50, cache_leitura=8000),
    ])
    eventos = await amb.conversar("quais obras?", obra_id=OBRA)

    usos = amb.api.feitas("POST", "/uso")
    assert len(usos) == 2
    u1, u2 = (u.corpo for u in usos)
    # Opus 5.5: US$ 4 entrada, 20 saída, 0,20 leitura de cache, 5 (=1,25 × 4) escrita, por milhão → µUS$ por token.
    assert u1["custo_microdolares"] == 1000 * 4 + 200 * 20 + 5000 * 0.20 + 3000 * 5 == 24_000
    assert u2["custo_microdolares"] == 300 * 4 + 50 * 20 + 8000 * 0.20 == 3_800
    assert (u1["tokens_entrada"], u1["tokens_saida"], u1["tokens_cache_leitura"], u1["tokens_cache_escrita"]) == (
        1000, 200, 5000, 3000)
    assert u1["modelo"] == "claude-opus-5-5" and u1["rota"] == "conversa" and u1["obra_id"] == OBRA
    assert u1["chave_propria"] is False and len(u1["dia"]) == 10
    assert (u1["tenant_id"], u1["usuario_id"]) == ("t1", "u1")
    assert u1["sessao_id"] == u2["sessao_id"] and u1["id"] != u2["id"]
    for u in usos:
        assert u.cabecalhos["authorization"] == f"Bearer {TOKEN_SERVICO}"
        assert u.cabecalhos["x-canteiro-usuario"] == TOKEN_USUARIO

    [(_, fim)] = [(e, d) for e, d in eventos if e == "fim"]
    assert fim["sessao_id"] == u1["sessao_id"]
    assert fim["uso"] == {"custo": 27_800, "tokens": 1000 + 200 + 5000 + 3000 + 300 + 50 + 8000}


async def test_sessao_guarda_o_historico_e_so_o_dono_le(ambiente):
    amb = ambiente([
        Resposta([ferramenta("listar_obras", {})], stop_reason="tool_use"),
        Resposta([texto("Casa Vila Nova.")]),
        Resposta([texto("Sim, a mesma.")]),
    ])
    ev1 = await amb.conversar("quais obras?")
    sessao_id = next(d for e, d in ev1 if e == "fim")["sessao_id"]
    await amb.conversar("é essa mesmo?", sessao_id=sessao_id)

    p3 = amb.anthropic.pedidos[2]
    papeis = [m["role"] for m in p3["messages"]]
    # usuário, modelo (tool_use), resultado, modelo (texto), usuário novo: o histórico só cresce.
    assert papeis == ["user", "assistant", "user", "assistant", "user"]
    assert p3["messages"][:4] == amb.anthropic.pedidos[1]["messages"] + [p3["messages"][3]]

    # Outra pessoa (a API aceita o token, mas é outro usuário) não continua a conversa alheia.
    amb.api.creditos = {**amb.api.creditos, "usuario_id": "outra-pessoa"}
    ev3 = await amb.conversar("me mostra", sessao_id=sessao_id)
    assert ev3 == [("erro", {"mensagem": "Conversa não encontrada."})]


async def test_recusa_do_modelo_vira_erro_amigavel(ambiente):
    amb = ambiente([Resposta([], stop_reason="refusal")])
    eventos = await amb.conversar("algo que o classificador recusa")
    nomes = [e for e, _ in eventos]
    assert nomes == ["erro", "fim"]
    assert len(amb.api.feitas("POST", "/uso")) == 1   # recusa também é medida


async def test_sem_login_nao_abre_conversa(ambiente):
    import httpx
    from canteiro_agente.main import criar_app

    amb = ambiente([])
    app = criar_app(amb.dep)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agente") as c:
        resp = await c.post("/conversa", json={"texto": "oi"})
    assert resp.status_code == 401
    assert amb.api.chamadas == []


async def test_ajuste_leva_o_motivo_como_justificativa(ambiente):
    amb = ambiente([
        Resposta([ferramenta("propor_ajuste_estoque", {
            "obra_id": OBRA, "insumo_id": ACO, "quantidade": "-12,5", "motivo": "sobra cortada perdida"})],
                 stop_reason="tool_use"),
        Resposta([texto("Proposta de ajuste aguardando aprovação.")]),
    ])
    eventos = await amb.conversar("perdemos 12,5 kg de aço")
    [proposta] = [d for e, d in eventos if e == "proposta"]
    assert proposta["titulo"] == "Ajustar Aço CA-50 em −12,5 kg"
    assert proposta["justificativa"] == "sobra cortada perdida"
    assert proposta["dados"] == {"insumo_id": ACO, "quantidade": -12_500, "motivo": "sobra cortada perdida"}
