"""Configuração por variável de ambiente. Nada de segredo em arquivo: o repositório é público."""
import os
from dataclasses import dataclass

MODELO_PADRAO = "claude-opus-5-5"

# Esforço por rota: a conversa é interativa (latência importa); o briefing é raro e pede mais cuidado.
ESFORCO_POR_ROTA: dict[str, str] = {
    "conversa": "medium",
    "briefing": "high",
}


@dataclass(frozen=True)
class Config:
    api_url: str
    # Chave da plataforma (clientes de créditos). Chave própria de cliente nunca passa por aqui.
    anthropic_api_key: str | None
    # Token de serviço do agente: só para `POST /uso`. Dados do cliente sempre com o token do usuário.
    token_servico: str | None
    modelo: str = MODELO_PADRAO
    # Demo hospedada sem chave: o modelo falso roda dentro do próprio processo (0 tokens, nada é cobrado da Anthropic).
    modelo_roteiro: bool = False
    max_tokens: int = 16000
    max_iteracoes: int = 12

    @classmethod
    def do_ambiente(cls) -> "Config":
        return cls(
            api_url=os.environ.get("API_URL", "http://localhost:8000").rstrip("/"),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            token_servico=os.environ.get("TOKEN_SERVICO_AGENTE") or None,
            modelo=os.environ.get("AGENTE_MODELO", MODELO_PADRAO),
            modelo_roteiro=os.environ.get("MODELO_ROTEIRO", "") == "1",
        )
