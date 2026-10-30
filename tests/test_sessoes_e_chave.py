"""Implementações HTTP de sessões e da chave própria contra a API falsa (mesmo contrato da API de verdade)."""
import logging

import pytest

from canteiro_agente.chaves import FonteChaveApi
from canteiro_agente.sessoes import HistoricoDivergiu, SessaoDeOutro, SessoesApi
from falsos import OBRA, TOKEN_OUTRO, TOKEN_SERVICO, TOKEN_USUARIO, ApiFalsa, Resposta, ferramenta, texto


def _repo():
    api = ApiFalsa()
    return api, SessoesApi(api.cliente(), "http://api.teste")


async def test_sessao_mapeia_role_content_para_papel_conteudo_e_volta():
    api, repo = _repo()
    s = await repo.abrir(None, dono="u1", autorizacao=TOKEN_USUARIO, obra_id=OBRA, titulo="aço")
    assert s.total == 0 and s.mensagens == []
    msgs = [{"role": "user", "content": [{"type": "text", "text": "oi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "olá"}]}]
    await repo.acrescentar(s, msgs, autorizacao=TOKEN_USUARIO)
    [post] = [c for c in api.chamadas if c.metodo == "POST" and c.caminho.endswith("/mensagens")]
    assert post.corpo == {"seq_inicial": 0, "mensagens": [
        {"papel": "user", "conteudo": [{"type": "text", "text": "oi"}]},
        {"papel": "assistant", "conteudo": [{"type": "text", "text": "olá"}]}]}
    assert post.cabecalhos["authorization"] == TOKEN_USUARIO

    de_novo = await repo.abrir(s.id, dono="u1", autorizacao=TOKEN_USUARIO)
    assert de_novo.mensagens == msgs and de_novo.total == 2
    assert api.sessoes[s.id]["obra_id"] == OBRA


async def test_lotes_de_50_e_leitura_paginada(monkeypatch):
    import canteiro_agente.sessoes as mod
    monkeypatch.setattr(mod, "PAGINA", 40)
    api, repo = _repo()
    s = await repo.abrir(None, dono="u1", autorizacao=TOKEN_USUARIO)
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(120)]
    await repo.acrescentar(s, msgs, autorizacao=TOKEN_USUARIO)
    lotes = [c.corpo["seq_inicial"] for c in api.chamadas if c.caminho.endswith("/mensagens") and c.metodo == "POST"]
    assert lotes == [0, 50, 100]
    lido = await repo.abrir(s.id, dono="u1", autorizacao=TOKEN_USUARIO)
    assert lido.mensagens == msgs
    assert len([c for c in api.chamadas if c.metodo == "GET"]) == 3     # 40 + 40 + 40


async def test_sessao_de_outra_pessoa_e_escrita_fora_de_ordem():
    api, repo = _repo()
    s = await repo.abrir(None, dono="u1", autorizacao=TOKEN_USUARIO)
    with pytest.raises(SessaoDeOutro):
        await repo.abrir(s.id, dono="u2", autorizacao=TOKEN_OUTRO)
    copia = await repo.abrir(s.id, dono="u1", autorizacao=TOKEN_USUARIO)
    await repo.acrescentar(s, [{"role": "user", "content": "a"}], autorizacao=TOKEN_USUARIO)
    with pytest.raises(HistoricoDivergiu):        # outra aba gravou antes: 409
        await repo.acrescentar(copia, [{"role": "user", "content": "b"}], autorizacao=TOKEN_USUARIO)


async def test_conversa_grava_so_as_mensagens_novas_na_api(ambiente):
    amb = ambiente([
        Resposta([ferramenta("listar_obras", {})], stop_reason="tool_use"),
        Resposta([texto("Casa Vila Nova.")]),
        Resposta([texto("Sim.")]),
    ])
    amb.dep.sessoes = SessoesApi(amb.api.cliente(), "http://api.teste")
    ev = await amb.conversar("quais obras?", obra_id=OBRA)
    sid = next(d for e, d in ev if e == "fim")["sessao_id"]
    await amb.conversar("é essa?", sessao_id=sid)

    gravacoes = [c.corpo for c in amb.api.chamadas if c.metodo == "POST" and c.caminho.endswith("/mensagens")]
    assert [(g["seq_inicial"], [m["papel"] for m in g["mensagens"]]) for g in gravacoes] == [
        (0, ["user", "assistant", "user", "assistant"]), (4, ["user", "assistant"])]
    # O modelo recebeu na 2ª conversa o histórico lido da API, em role/content.
    assert [m["role"] for m in amb.anthropic.pedidos[2]["messages"]] == ["user", "assistant", "user", "assistant", "user"]
    [criacao, *_] = [c.corpo for c in amb.api.chamadas if c.caminho == "/sessoes"]
    assert criacao["titulo"] == "quais obras?" and criacao["obra_id"] == OBRA


async def test_chave_propria_vem_da_rota_interna_e_nao_vai_para_log(caplog):
    api = ApiFalsa(chave_cliente="sk-ant-cliente-secreta")
    fonte = FonteChaveApi(api.cliente(), "http://api.teste", TOKEN_SERVICO)
    with caplog.at_level(logging.DEBUG):
        assert await fonte.chave_do_cliente(TOKEN_USUARIO) == "sk-ant-cliente-secreta"
    [c] = api.chamadas
    assert c.caminho == "/interno/chave-anthropic"
    assert c.cabecalhos["authorization"] == f"Bearer {TOKEN_SERVICO}"
    assert c.cabecalhos["x-canteiro-usuario"] == TOKEN_USUARIO
    assert "secreta" not in caplog.text

    assert await FonteChaveApi(ApiFalsa().cliente(), "http://api.teste", TOKEN_SERVICO).chave_do_cliente(
        TOKEN_USUARIO) is None                                       # 404 → sem chave


async def test_conversa_com_chave_propria_da_api(ambiente):
    amb = ambiente([Resposta([texto("ok")])])
    amb.api.creditos = {"saldo_microdolares": 0, "franquia": 0, "chave_propria": True,
                        "usuario_id": "u1", "tenant_id": "t1"}
    amb.api.chave_cliente = "chave-do-cliente"
    amb.dep.fonte_chave = FonteChaveApi(amb.api.cliente(), "http://api.teste", TOKEN_SERVICO)
    eventos = await amb.conversar("oi")
    assert [e for e, _ in eventos][-1] == "fim"
    assert amb.anthropic.chaves == ["chave-do-cliente"]
