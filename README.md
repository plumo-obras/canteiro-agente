# canteiro-agente

O agente de IA do **Plumo**, gestão de obras para engenheiros, mestres e compradores. Responde em conversa sobre a
obra (estoque, pedidos, orçamento, cronograma), propõe compras, entregas e ajustes, e lê comprovantes de gasto. Tudo
com os dados que a API devolve: o agente não faz conta de cabeça e **não executa nada**, só propõe.

## Onde ele fica

O produto são três repositórios:

| Repositório | O que é | Onde roda |
|---|---|---|
| [canteiro-api](https://github.com/plumo-obras/canteiro-api) | FastAPI: domínio (`canteiro-dominio`), banco, papéis, propostas, créditos de IA, histórico das conversas | Render |
| **canteiro-agente** (este) | o agente: `POST /conversa` (SSE) e `POST /leituras/comprovante` | Render |
| [canteiro-web](https://github.com/plumo-obras/canteiro-web) | Next.js: telas, chat, aprovação das propostas | Vercel |

```
web ── POST /conversa (token do usuário) ──> agente ── ferramentas, com o MESMO token ──> API ──> banco
 ▲                                             │
 └──────────── eventos SSE ────────────────────┘        escrita do agente = POST /propostas (só isso)
```

- **O agente não fala com o banco.** Toda leitura e toda proposta passam pela API, com o token de quem está
  conversando; tenant e papel a API resolve a cada requisição.
- O token de serviço do agente só grava consumo (`POST /uso`) e a leitura de um comprovante, sempre junto com o token
  do usuário (cabeçalho `X-Canteiro-Usuario`).
- O contrato entre os três e a arquitetura moram no canteiro-api (`docs/contrato.md`, `docs/arquitetura.md`). O
  domínio também vem de lá, fixado por tag no `pyproject.toml`.

## Como funciona

### A conversa (`conversa.py`)

Uma pergunta é uma **volta**:

1. `GET /creditos` antes de qualquer token. Sem saldo, nem começa. Cliente com chave própria usa a chave dele, que vem
   cifrada da API (`chaves.py`); sem ela a volta termina com erro, nunca cai na chave da plataforma.
2. Abre a sessão (`sessoes.py`) e lê o histórico guardado na API. A data de hoje (Brasília) e a obra em foco vão na
   **mensagem do usuário**, não na instrução.
3. Roda o **Tool Runner** do SDK da Anthropic em streaming (`client.beta.messages.tool_runner`): o modelo chama as
   ferramentas, recebe o resultado, segue. Até 12 passos.
4. Cada resposta do modelo grava `POST /uso` na hora (tokens e custo em microdólares, `precos.py`). Se a conexão cai
   no meio, o uso parcial somado pelos eventos do stream é gravado mesmo assim.
5. O histórico é gravado a cada rodada de ferramentas completa: uma proposta criada antes de a aba fechar já está lá,
   e a próxima pergunta não a propõe de novo.

Eventos SSE para a tela: `texto`, `descartar`, `ferramenta`, `proposta`, `fim`, `erro`, e o comentário `: ping` nos
silêncios (proxy e função da Vercel derrubam conexão parada).

### Ferramentas (`ferramentas.py`)

13 ferramentas, criadas por requisição (fecham sobre o token da conversa), mas com nome, descrição e esquema sempre
nos mesmos bytes.

- **Leitura:** `listar_obras`, `resumo_obra`, `consultar_estoque`, `historico_movimentos`, `listar_pedidos`,
  `consultar_orcamento`, `consultar_cronograma`, `listar_fornecedores`. Devolvem o número bruto e o formatado, e a
  linha de conta pronta para citar.
- **Calculadora:** `calcular` (`calculo.py`). Toda conta que não veio pronta passa por ela: Decimal, árvore sintática
  restrita, sem `eval`. Devolve "7 m³ × R$ 130,00/m³ = R$ 910,00".
- **Proposta:** `propor_pedido`, `propor_entrega`, `propor_avanco_pedido`, `propor_ajuste_estoque`. Criam
  `POST /propostas`, que uma pessoa aprova ou recusa na web; aprovar gera entrada no registro com desfazer. Cada
  proposta leva a chave `"{sessao_id}:{tool_use_id}"`: repetir a mesma chamada devolve a proposta que já existe
  (`ja_existia`).

`tests/test_so_propoe.py` falha se surgir qualquer outra escrita.

### Memória, cache e contexto

- **Histórico:** em `sessoes`/`mensagens` na API, por `sessao_id`, só acrescentado (a API recusa com 409 lote fora de
  sequência). Mensagem antiga nunca é reescrita: invalidaria o cache e os blocos de raciocínio.
- **Prompt caching:** um ponto de cache no fim da instrução cobre ferramentas + instrução; o cache automático cobre o
  histórico. Por isso a instrução (`instrucao.py`) não tem data, nome nem ID.
- **Context management:** acima de 150 mil tokens de entrada, o servidor limpa resultados antigos de ferramenta
  (`clear_tool_uses`), mantendo os 8 últimos. Os `propor_*` nunca são limpos: o modelo precisa ver o que já propôs.
- **Lembretes uma vez por conversa:** aviso de "sem previsão lançada" ou data futura num documento tem chave. A
  ferramenta manda o texto na primeira vez (`lembretes`) e só a chave depois (`lembretes_ja_ditos`). Quem sabe o que
  já foi dito é o código, lendo os `tool_result` guardados.

### Limites e falhas

- **Uma volta por sessão de cada vez** (trava no processo; o agente roda numa instância só). Com mais de uma, o 409
  da API na gravação do histórico segura a ordem.
- **Tempo:** 240 s por volta, abaixo dos 300 s da função da web que faz o proxy. Estourou, a tela recebe aviso e o
  que foi feito fica salvo.
- **API fora do ar** no meio das ferramentas: a volta para (sem pagar outra chamada ao modelo) e avisa.
- **Recusa** de classificador de segurança é refeita no servidor em outro modelo (`fallbacks="default"`), com o custo
  no preço de quem respondeu.

### Leitura de comprovante (`comprovantes.py`)

`POST /leituras/comprovante {comprovante_id}`, JSON. Não é agente: fluxo fixo, **uma** chamada ao Claude, sem
ferramentas, com saída estruturada no esquema `LeituraComprovante` do domínio. Confere créditos, busca o comprovante,
o arquivo (foto ou PDF), a árvore de etapas e a obra, lê e grava em `PUT /interno/comprovantes/{id}/leitura`. O
consumo é gravado **antes** de validar a resposta. É sugestão: o gasto só nasce quando uma pessoa confirma na revisão.
Falha vira frase gravada no comprovante, e a revisão abre em branco para preencher à mão.

## Modelo e modo roteiro

- Modelo padrão `claude-opus-5-5` (`config.py`), pensamento adaptativo, esforço `medium` na conversa. Trocar com
  `AGENTE_MODELO`; o modelo precisa ter preço em `precos.PRECOS`, senão o serviço não sobe.
- **Modo roteiro** (`MODELO_ROTEIRO=1`): o SDK de verdade conversa com um modelo **falso** (`modelo_roteiro.py`) dentro
  do próprio processo. Zero token, nada gravado em `/uso`. É o padrão do `.env.exemplo` e do Render enquanto não há
  chave. Não lê comprovante (isso exige o Claude).

O roteiro responde pela última mensagem de texto do usuário:

| A pessoa fala de | O "modelo" faz |
|---|---|
| bloco / "2.760" | `consultar_estoque` → `historico_movimentos` → cita a conta e a quebra. Não propõe. |
| cimento | `consultar_estoque` → `propor_pedido` do que falta → texto curto |
| aço | `consultar_estoque` → "já está em cotação, não precisa pedir" |
| outra coisa | "Não sei com segurança." |

Os números saem do `tool_result`, não de constantes: muda o seed, muda a resposta. O `usage` é plausível (a 1ª volta
escreve o prefixo no cache, as seguintes leem).

Também roda como servidor separado, para apontar o agente com `ANTHROPIC_BASE_URL`:

```bash
.venv/bin/python -m canteiro_agente.modelo_roteiro --porta 8199
ANTHROPIC_BASE_URL=http://localhost:8199 ANTHROPIC_API_KEY=falsa MODELO_ROTEIRO=0 \
  .venv/bin/uvicorn canteiro_agente.main:app --port 8100
```

## Rodar local

```bash
uv venv -p 3.14 .venv && uv pip install -p .venv -e ".[dev]"   # o domínio vem do canteiro-api, pela tag
cp .env.exemplo .env && set -a && . ./.env && set +a
.venv/bin/uvicorn canteiro_agente.main:app --reload --port 8100
```

Precisa da canteiro-api rodando em `API_URL`. O produto inteiro sobe pelo `docker-compose.completo.yml` do
canteiro-api. Em contêiner: `docker build -t canteiro-agente . && docker run --env-file .env -p 8100:8100 canteiro-agente`.

Mexendo no domínio ao mesmo tempo (canteiro-api clonado ao lado), use a cópia local no lugar da tag:

```bash
uv pip install -p .venv --no-sources -e ../canteiro-api/dominio -e ".[dev]"
```

Versão nova do domínio (tag `dominio-vX.Y.Z` no canteiro-api): o Dependabot não acompanha fonte git por tag, então é
à mão. Troque o `tag` em `[tool.uv.sources]` no `pyproject.toml`, rode `uv lock`, depois `uv sync --frozen --extra
dev`, `.venv/bin/pytest` e PR. Tags: `git ls-remote --tags https://github.com/plumo-obras/canteiro-api 'dominio-v*'`.

## Testes e lint

```bash
.venv/bin/pytest                    # sem rede e sem gastar token
.venv/bin/ruff check src tests
```

Os testes usam dublês dos dois lados (`tests/falsos.py`): `ApiFalsa` (a API central com o seed, via
`httpx.MockTransport`) e `AnthropicFalsa` (respostas SSE roteirizadas). O cliente `AsyncAnthropic` e o Tool Runner são
os de verdade; só o transporte é falso.

Teste com o modelo real **gasta token**: marcado `modelo`, pulado por padrão, só com autorização:
`RODAR_COM_MODELO=1 ANTHROPIC_API_KEY=… .venv/bin/pytest -m modelo`.

## Variáveis de ambiente

Só nomes; os valores ficam no `.env` (fora do git) e no painel do Render. Veja `.env.exemplo` e `config.py`.

| Variável | Para quê |
|---|---|
| `API_URL` | URL da canteiro-api |
| `ANTHROPIC_API_KEY` | chave da **plataforma** (clientes de créditos). Chave própria de cliente vem da API, nunca daqui |
| `TOKEN_SERVICO_AGENTE` | token de serviço: só `POST /uso` e gravar leitura de comprovante. Mesmo valor que a API lê |
| `MODELO_ROTEIRO` | `1` = modelo de roteiro (0 token); `0` = Claude de verdade |
| `AGENTE_MODELO` | opcional; troca o modelo (precisa de preço em `precos.py`) |
| `AGENTE_TEMPO_MAX_VOLTA` | opcional; teto de uma volta em segundos (padrão 240) |
| `AGENTE_INTERVALO_PING` | opcional; silêncio máximo do SSE antes do ping (padrão 15) |
| `ANTHROPIC_BASE_URL` | só em desenvolvimento, para apontar o SDK para o roteiro como servidor separado |

## Deploy

Render, plano gratuito, Docker (`render.yaml`, `Dockerfile`). `autoDeployTrigger: checksPass`: push na `main` só
publica depois que o CI (`.github/workflows/ci.yml`) passa: pytest, ruff, build da imagem e o contêiner respondendo
`/saude`. Segredos ficam no painel (`sync: false`). O serviço gratuito dorme após 15 min sem uso e leva uns 30 a 60 s
para acordar.

## Estrutura

```
src/canteiro_agente/
  main.py            FastAPI: /conversa (SSE), /leituras/comprovante, /saude
  conversa.py        a volta: créditos → Tool Runner → uso → histórico; trava por sessão, timeout, ping
  ferramentas.py     as 13 ferramentas (leitura, calcular, propor_*), lembretes, idempotência
  instrucao.py       a instrução do agente (texto fixo: é o prefixo em cache)
  api_central.py     cliente da API com o token do usuário; ApiForaDoAr; validação de id
  calculo.py         a calculadora (Decimal, AST restrita)
  comprovantes.py    leitura de comprovante (uma chamada, saída estruturada)
  sessoes.py         histórico por sessao_id (API em produção, memória nos testes)
  chaves.py          de onde vem a chave da Anthropic (plataforma ou própria)
  precos.py          custo por modelo em microdólares
  modelo_roteiro.py  modelo falso da API de mensagens (0 token)
  config.py          configuração por variável de ambiente
tests/               dublês em falsos.py; test_so_propoe.py é a garantia estrutural
docs/desenho-agente.md
.claude/skills/      guias para o Claude Code mexer neste repositório
```

## Leia também

- [`CLAUDE.md`](CLAUDE.md): as regras da casa.
- [`docs/desenho-agente.md`](docs/desenho-agente.md): o que o agente faz hoje, os trabalhos que vêm (alertas,
  briefing, ingestão), memória e avaliação.
- `.claude/skills/`: como criar ferramenta, tom das respostas, prompt e custo, segurança, testes sem modelo, leitura
  de comprovante.
- canteiro-api: `docs/contrato.md` e `docs/arquitetura.md`.

O repositório é **público**: nada de chave, token, e-mail ou senha em arquivo, teste ou log.
