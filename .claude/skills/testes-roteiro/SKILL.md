---
name: testes-roteiro
description: Use ao escrever ou rodar teste do agente, ao reproduzir um bug de conversa, ao demonstrar o agente sem chave, ou quando alguém sugerir "testa com o modelo de verdade". Cobre tests/falsos.py (ApiFalsa, AnthropicFalsa, Resposta, ferramenta, texto, Crua), o fixture ambiente do conftest, o modelo de roteiro (MODELO_ROTEIRO=1, modelo_roteiro.py) e o teste com modelo real (RODAR_COM_MODELO=1), que gasta token e exige autorização.
---

# Testar sem gastar token

O dia a dia não chama a Anthropic. O SDK e o Tool Runner rodam de verdade; só o transporte é falso.

## As peças

- `tests/falsos.py`
  - `ApiFalsa`: a API central do contrato, com o seed (Casa Vila Nova, EMEI), por `httpx.MockTransport`. Registra
    tudo em `chamadas`; `feitas("POST", "/propostas")` filtra. Liga falhas: `creditos_status`, `fora_do_ar_depois`,
    `chave_cliente`.
  - `AnthropicFalsa`: responde `/v1/messages` em SSE com uma lista de respostas na ordem. `amb.anthropic.pedidos`
    guarda o que foi enviado ao modelo (para conferir `system`, `tools`, `tool_result`).
  - `Resposta([...], stop_reason=...)`, `texto("...")`, `ferramenta(nome, entrada, id_="toolu_1")`.
  - `Crua`, `pausada(...)`, `json_ilegivel(...)`: stream parado no meio (timeout, cancelamento, ping) e JSON quebrado.
- `tests/conftest.py`: fixture `ambiente` → `amb = ambiente([Resposta(...), ...])`; `await amb.conversar(texto,
  obra_id=..., sessao_id=...)` devolve a lista `(evento, dados)` lida do SSE.

## Receitas

Ponta a ponta (conversa com ferramenta):

```python
amb = ambiente([
    Resposta([ferramenta("consultar_estoque", {"obra_id": OBRA, "busca": "cimento"})], stop_reason="tool_use"),
    Resposta([texto("Falta **34 sc** de cimento.")]),
])
eventos = await amb.conversar("quanto falta de cimento?", obra_id=OBRA)
```

Confira eventos (`ferramenta`, `proposta`, `fim`, `erro`), o que foi à API (`amb.api.feitas(...)`, cabeçalho
`authorization` = token do usuário), o `/uso` gravado e o que o modelo recebeu (`amb.anthropic.pedidos`).

Ferramenta isolada: `criar_ferramentas(ApiCentral(http, "http://api", TOKEN_USUARIO), Eventos(hoje=HOJE))` e
`await fs[nome].call(entrada)` (veja `tests/test_ferramentas.py`). Fixe `hoje` para data relativa.

Comprovante: `tests/test_comprovantes.py` (resposta não transmitida, saída estruturada).

## Modo roteiro (demo e teste manual)

- `MODELO_ROTEIRO=1`: o agente usa `modelo_roteiro.py` dentro do processo (0 token, sem `/uso`). Responde por
  intenção (`intencao()`: bloco, cimento, aço, outra coisa) e tira os números do `tool_result`.
- Como servidor: `.venv/bin/python -m canteiro_agente.modelo_roteiro --porta 8199` e o agente com
  `ANTHROPIC_BASE_URL=http://localhost:8199 MODELO_ROTEIRO=0`.
- Intenção nova na demo: `intencao()` + `roteiro()` + caso em `tests/test_modelo_roteiro.py`. O roteiro não faz conta
  que o agente de verdade não faria.

## Modelo real: só com autorização

- `tests/test_integracao_modelo.py`, marcado `modelo`, pulado sem `RODAR_COM_MODELO=1`. Gasta token da conta de quem
  roda. **Peça autorização ao humano antes**, diga quantos casos e o que quer medir.
- `RODAR_COM_MODELO=1 ANTHROPIC_API_KEY=… .venv/bin/pytest -m modelo`. A chave vem do ambiente, nunca de arquivo.
- Asserção sobre o que veio da ferramenta (número da API, ferramenta chamada), não sobre a frase exata.

## Checklist

- [ ] Teste novo roda sem rede e sem chave (`.venv/bin/pytest`).
- [ ] Usa valores falsos de token e chave; nenhum segredo real.
- [ ] Bug de conversa reproduzido com `Resposta` roteirizada antes do conserto.
- [ ] Falhas cobertas quando cabem: API fora, 401/403/404, sem crédito, timeout, JSON ilegível.
- [ ] `.venv/bin/ruff check src tests` verde.
- [ ] Nada com `RODAR_COM_MODELO=1` sem o "ok" explícito do humano.
