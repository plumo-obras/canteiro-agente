"""Leitura de comprovante: créditos → arquivo e árvore → uma chamada ao modelo → leitura (ou erro) gravada na API.

Sem rede e sem token: a API central e a Anthropic são transportes falsos; o cliente `AsyncAnthropic` e o `parse` do
SDK são os de verdade.
"""
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import httpx2
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from canteiro_agente.chaves import SemChaveCliente
from canteiro_agente.comprovantes import (
    ERRO_CHAVE_PROPRIA,
    ERRO_RECUSA,
    ERRO_SEM_CREDITO,
    INSTRUCAO_LEITURA,
)
from canteiro_agente.config import Config
from canteiro_agente.conversa import Dependencias
from canteiro_agente.main import criar_app
from canteiro_agente.sessoes import SessoesEmMemoria

from falsos import TOKEN_SERVICO, TOKEN_USUARIO

FOTO = b"\xff\xd8\xff\xe0foto do pix"
LEITURA = {
    "tipo_documento": "comprovante_pix", "situacao_pagamento": "pago",
    "valor": {"certeza": "alta", "opcoes": [{"valor": "350,00", "motivo": "valor do Pix"}]},
    "data_pagamento": {"certeza": "alta", "opcoes": [{"valor": "2026-11-03", "motivo": "data do Pix"}]},
    "favorecido": {"certeza": "alta", "opcoes": [{"valor": "João da Silva", "motivo": "recebedor"}]},
    "cnpj_favorecido": None,
    "descricao": {"certeza": "baixa", "opcoes": [{"valor": "mão de obra do eletricista", "motivo": "legenda"}]},
    "classificacao": {"certeza": "alta", "opcoes": [{"etapa": "eletrica", "topico_id": "t1", "motivo": "legenda"}]},
    "observacoes": None,
}


@dataclass
class Api:
    creditos_status: int = 200
    chave_propria: bool = False
    status_comprovante: str = "a_revisar"
    gravadas: list[dict[str, Any]] = field(default_factory=list)
    usos: list[dict[str, Any]] = field(default_factory=list)
    cabecalhos_internos: list[dict[str, str]] = field(default_factory=list)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        if p == "/creditos":
            if self.creditos_status != 200:
                return httpx.Response(self.creditos_status, json={"detail": "Os créditos de IA acabaram."})
            return httpx.Response(200, json={"saldo_microdolares": 5_000_000, "chave_propria": self.chave_propria,
                                             "tenant_id": "t1", "usuario_id": "u1"})
        if p == "/comprovantes/c1" and m == "GET":
            return httpx.Response(200, json={"id": "c1", "obra_id": "o1", "status": self.status_comprovante,
                                             "tipo_conteudo": "image/jpeg", "legenda": "eletricista, quadro"})
        if p == "/comprovantes/c1/arquivo":
            return httpx.Response(200, content=FOTO, headers={"content-type": "image/jpeg"})
        if p == "/obras/o1/arvore":
            return httpx.Response(200, json=[
                {"chave": "eletrica", "nome": "Instalações elétricas", "inclui": "", "fixa": True,
                 "topicos": [{"id": "t1", "nome": "Quadro geral"}]},
                {"chave": "x-e1", "nome": "Muro de arrimo", "inclui": "", "fixa": False, "topicos": []},
            ])
        if p == "/obras":
            return httpx.Response(200, json=[{"id": "o1", "nome": "Edifício Teste", "cidade": "Juiz de Fora",
                                              "uf": "MG", "tipologia": "predio"}])
        if p == "/interno/comprovantes/c1/leitura" and m == "PUT":
            self.cabecalhos_internos.append(dict(req.headers))
            corpo = json.loads(req.content)
            self.gravadas.append(corpo)
            return httpx.Response(200, json={"id": "c1", **corpo})
        if p == "/uso" and m == "POST":
            self.usos.append(json.loads(req.content))
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"detail": f"rota falsa ausente: {m} {p}"})


@dataclass
class Claude:
    stop_reason: str = "end_turn"
    pedidos: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        self.pedidos.append(json.loads(req.content))
        conteudo = [] if self.stop_reason == "refusal" else [{"type": "text", "text": json.dumps(LEITURA)}]
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": conteudo,
            "stop_reason": self.stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 3000, "output_tokens": 400, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0},
        })


def _cliente(claude: Claude) -> AsyncAnthropic:
    return AsyncAnthropic(api_key="chave-falsa", max_retries=0,
                          http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(claude)))


async def _ler(api: Api, claude: Claude, autorizacao: str | None = TOKEN_USUARIO, **config) -> httpx.Response:
    dep = Dependencias(
        config=Config(api_url="http://api.teste", anthropic_api_key="chave-plataforma", token_servico=TOKEN_SERVICO,
                      **config),
        http_api=httpx.AsyncClient(transport=httpx.MockTransport(api)),
        sessoes=SessoesEmMemoria(),
        fonte_chave=SemChaveCliente(),
        cliente_plataforma=_cliente(claude),
        fabrica_cliente_proprio=lambda chave: _cliente(claude),
    )
    app = criar_app(dep)
    cab = {"Authorization": autorizacao} if autorizacao else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agente") as c:
        return await c.post("/leituras/comprovante", json={"comprovante_id": "c1"}, headers=cab)


async def test_le_grava_a_leitura_e_o_consumo():
    api, claude = Api(), Claude()
    r = await _ler(api, claude)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert api.gravadas == [{"leitura": LEITURA, "modelo": "claude-opus-5-5"}]
    # rota interna: token de serviço + o do usuário, nunca um tenant informado pelo agente
    assert api.cabecalhos_internos[0]["authorization"] == f"Bearer {TOKEN_SERVICO}"
    assert api.cabecalhos_internos[0]["x-canteiro-usuario"] == TOKEN_USUARIO
    uso = api.usos[0]
    assert (uso["rota"], uso["obra_id"], uso["tenant_id"], uso["tokens_entrada"]) == ("comprovante", "o1", "t1", 3000)
    assert uso["custo_microdolares"] > 0

    pedido = claude.pedidos[0]
    assert pedido["fallbacks"] == "default"
    assert pedido["output_config"]["effort"] == "medium"
    assert pedido["output_config"]["format"]["type"] == "json_schema"
    instrucao, contexto = pedido["system"]
    assert instrucao["text"] == INSTRUCAO_LEITURA and instrucao["cache_control"] == {"type": "ephemeral"}
    assert "eletrica | t1 | Quadro geral" in contexto["text"] and "x-e1 | Muro de arrimo" in contexto["text"]
    assert "Edifício Teste (predio, Juiz de Fora/MG)" in contexto["text"]
    arquivo, texto = pedido["messages"][0]["content"]
    assert arquivo["type"] == "image" and arquivo["source"]["media_type"] == "image/jpeg"
    assert "eletricista, quadro" in texto["text"]


def test_instrucao_traz_todas_as_etapas_fixas_e_e_estavel():
    # o bloco é o mesmo em toda chamada (é o que fica em cache): nada de data, obra ou usuário dentro dele
    assert "pessoal | Pessoal e encargos" in INSTRUCAO_LEITURA and "outros | Outros" in INSTRUCAO_LEITURA
    assert "Edifício" not in INSTRUCAO_LEITURA


async def test_recusa_vira_erro_gravado_e_o_consumo_conta():
    api = Api()
    r = await _ler(api, Claude(stop_reason="refusal"))
    assert r.json() == {"ok": False, "mensagem": ERRO_RECUSA, "comprovante": {"id": "c1", "erro": ERRO_RECUSA}}
    assert api.gravadas == [{"erro": ERRO_RECUSA}]
    assert len(api.usos) == 1      # a recusa também foi cobrada


async def test_sem_credito_nao_chama_o_modelo():
    api, claude = Api(creditos_status=402), Claude()
    r = await _ler(api, claude)
    assert r.json()["mensagem"] == ERRO_SEM_CREDITO
    assert api.gravadas == [{"erro": ERRO_SEM_CREDITO}] and claude.pedidos == [] and api.usos == []


async def test_chave_propria_indisponivel_nao_usa_a_da_plataforma():
    api, claude = Api(chave_propria=True), Claude()
    r = await _ler(api, claude)
    assert r.json()["mensagem"] == ERRO_CHAVE_PROPRIA and claude.pedidos == []


async def test_comprovante_ja_revisado_nao_e_lido():
    api, claude = Api(status_comprovante="confirmado"), Claude()
    r = await _ler(api, claude)
    assert r.json()["ok"] is False and claude.pedidos == [] and api.gravadas == []


async def test_modo_roteiro_nao_le_foto():
    api, claude = Api(), Claude()
    r = await _ler(api, claude, modelo_roteiro=True)
    assert r.json()["ok"] is False and claude.pedidos == [] and len(api.gravadas) == 1


async def test_sem_login_e_401():
    r = await _ler(Api(), Claude(), autorizacao=None)
    assert r.status_code == 401
