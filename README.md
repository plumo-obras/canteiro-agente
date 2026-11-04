# canteiro-agente

Serviço do agente: `POST /conversa` em SSE, ferramentas que chamam a API central com o token do usuário e só
propõem escrita. Contrato: [`docs/contrato.md`](https://github.com/plumo-obras/canteiro-api/blob/main/docs/contrato.md) no canteiro-api.

```bash
uv venv -p 3.14 .venv && uv pip install -p .venv -e ".[dev]"   # o domínio vem do canteiro-api, pela tag
cp .env.exemplo .env && set -a && . ./.env && set +a              # API_URL, TOKEN_SERVICO_AGENTE, ANTHROPIC_API_KEY
.venv/bin/uvicorn canteiro_agente.main:app --reload --port 8100
.venv/bin/pytest                          # sem rede e sem gastar token
```

Mexendo no domínio ao mesmo tempo (canteiro-api clonado ao lado), use a cópia local no lugar da tag:

```bash
uv pip install -p .venv --no-sources -e ../canteiro-api/dominio -e ".[dev]"
```

Versão nova do domínio (tag `dominio-vX.Y.Z` publicada no canteiro-api): o Dependabot não acompanha fonte git por
tag, então é à mão. Troque o `tag` em `[tool.uv.sources]` no `pyproject.toml` e rode `uv lock` (trava o commit novo
no `uv.lock`); depois `uv sync --frozen --extra dev`, `.venv/bin/pytest` e PR. Tags existentes:
`git ls-remote --tags https://github.com/plumo-obras/canteiro-api 'dominio-v*'`.

Em contêiner: `docker build -t canteiro-agente . && docker run --env-file .env -p 8100:8100 canteiro-agente`. O produto
inteiro sobe pelo `docker-compose.completo.yml` do canteiro-api.

## Ponta a ponta sem gastar token: o modelo de roteiro

`canteiro_agente.modelo_roteiro` é um servidor FALSO da API de mensagens da Anthropic. Responde em streaming no
mesmo formato da API real, então o SDK e o Tool Runner rodam de verdade; só as respostas são roteirizadas.

```bash
.venv/bin/python -m canteiro_agente.modelo_roteiro --porta 8199
ANTHROPIC_BASE_URL=http://localhost:8199 ANTHROPIC_API_KEY=falsa \
  .venv/bin/uvicorn canteiro_agente.main:app --port 8100     # com API_URL e TOKEN_SERVICO_AGENTE da API real
```

Roteiro, pela última mensagem de texto do usuário:

| A pessoa fala de | O "modelo" faz |
|---|---|
| bloco / "2.760" | `consultar_estoque` → `historico_movimentos` → cita a conta e a quebra (≈15% no seed). Não propõe. |
| cimento | `consultar_estoque` → `propor_pedido` do que falta (34 sc no seed) → texto curto |
| aço | `consultar_estoque` → "os 600 kg já estão em cotação, falta 0, não precisa pedir" |
| outra coisa | "Não sei com segurança." |

- Sem obra em foco, começa por `listar_obras` e usa a primeira.
- Os números saem do `tool_result` que o agente devolveu, não de constantes: muda o seed, muda a resposta.
- Fornecedor e valor do pedido de cimento vêm do texto ("na Votorantim por R$ 1.360,00"). Se não aparecerem, usa
  Votorantim / 1.360,00.
- `usage` plausível: cerca de 4 caracteres por token. A 1ª volta escreve o prefixo (ferramentas + instrução) no cache e
  as seguintes leem. Assim `/uso` e os créditos andam como na vida real.
- O percentual de quebra é calculado pelo roteiro só para a demo. O agente de verdade não faz conta: se a tela precisar
  desse índice, ele deve vir pronto da API.
- Não valida chave nem tem raciocínio. Só para a máquina de desenvolvimento.
