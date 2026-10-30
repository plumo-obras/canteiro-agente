import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from canteiro_agente.chaves import SemChaveCliente  # noqa: E402
from canteiro_agente.config import Config  # noqa: E402
from canteiro_agente.conversa import Dependencias  # noqa: E402
from canteiro_agente.main import criar_app  # noqa: E402
from canteiro_agente.sessoes import SessoesEmMemoria  # noqa: E402
from falsos import TOKEN_SERVICO, TOKEN_USUARIO, AnthropicFalsa, ApiFalsa  # noqa: E402


class Ambiente:
    def __init__(self, roteiro):
        self.api = ApiFalsa()
        self.anthropic = AnthropicFalsa(roteiro)
        self.dep = Dependencias(
            config=Config(api_url="http://api.teste", anthropic_api_key="chave-plataforma", token_servico=TOKEN_SERVICO),
            http_api=self.api.cliente(),
            sessoes=SessoesEmMemoria(),
            fonte_chave=SemChaveCliente(),
            cliente_plataforma=self.anthropic.cliente("chave-plataforma"),
            fabrica_cliente_proprio=lambda chave: self.anthropic.cliente(chave),
        )

    async def conversar(self, texto, *, sessao_id=None, obra_id=None, autorizacao=TOKEN_USUARIO):
        """Chama POST /conversa e devolve a lista de (evento, dados) lida do SSE."""
        app = criar_app(self.dep)
        corpo = {"texto": texto, "sessao_id": sessao_id, "obra_id": obra_id}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agente") as c:
            resp = await c.post("/conversa", json=corpo, headers={"Authorization": autorizacao})
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/event-stream")
        eventos = []
        for bloco in resp.text.strip().split("\n\n"):
            linhas = dict(l.split(": ", 1) for l in bloco.splitlines())
            eventos.append((linhas["event"], json.loads(linhas["data"])))
        return eventos


@pytest.fixture
def ambiente():
    return Ambiente
