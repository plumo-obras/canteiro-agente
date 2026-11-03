# canteiro-agente

Serviço do agente do Canteiro (agente de IA para gestão de obras): conversa por SSE, Claude + Tool Runner, ferramentas
que chamam a [canteiro-api](https://github.com/plumo-obras/canteiro-api) com o token do usuário e **só propõem** escrita.
A web é o [canteiro-web](https://github.com/plumo-obras/canteiro-web).

O contrato entre API, agente e web e a arquitetura moram no canteiro-api (`docs/contrato.md`, `docs/arquitetura.md`).
Leia antes de codar. O domínio (`canteiro-dominio`) também vem de lá, fixado por tag no `pyproject.toml`.

## Regras da casa

**O repositório é PÚBLICO.** Nada de segredo em arquivo, teste ou log: `.env` fica fora do git (há `.env.exemplo`);
chave da Anthropic da plataforma só em variável de ambiente; chave própria de cliente cifrada no banco. Pesquisa de
mercado e estratégia ficam em `pesquisa/` (ignorada pelo git).

**O agente não fala com o banco.** Tudo passa pela API central, com o token do usuário.

**Toda conta no domínio, nunca no modelo de IA.** Falta, saldo, transições: `canteiro_dominio.regras`. O agente cita a
`Conta` devolvida pela API.

**O agente só propõe.** Escrita do agente = `POST /propostas`. Só a aprovação de uma pessoa executa, e toda execução gera
`EntradaRegistro` com `desfazer`. Não crie caminho de escrita do agente que pule isso.

**Multi-tenant e papel no servidor.** `tenant_id` em todo documento e em toda consulta (a porta exige). O papel vem do
banco a cada requisição, nunca do token. O agente age com o token do usuário.

**Consumo de IA medido.** Toda chamada ao modelo grava `UsoIA`; o saldo de créditos é checado antes da chamada.

**Comentário explica por quê, não o quê.** Português do Brasil no código de domínio e nas mensagens.

**Testes com o modelo de verdade gastam tokens.** Ficam marcados `modelo` e só rodam com `RODAR_COM_MODELO=1`, com
autorização. O dia a dia usa o modelo de roteiro (`MODELO_ROTEIRO=1`, 0 tokens).

## Comandos

```bash
uv venv -p 3.14 .venv && uv pip install -p .venv -e ".[dev]"
.venv/bin/pytest                                           # sem rede e sem gastar token
.venv/bin/uvicorn canteiro_agente.main:app --reload --port 8100
docker build -t canteiro-agente . && docker run --env-file .env -p 8100:8100 canteiro-agente
```
