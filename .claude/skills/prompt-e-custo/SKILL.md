---
name: prompt-e-custo
description: Use ao mexer em qualquer coisa que muda o que vai ao modelo ou quanto custa - instrucao.py, o bloco system ou os parâmetros do tool_runner em conversa.py, GESTAO_DE_CONTEXTO, lembretes, precos.py, registro de uso (POST /uso), chave própria (chaves.py), troca de modelo ou esforço (config.py). Gatilhos: "cache não está pegando", "custo subiu", "trocar o modelo", "o agente repete o lembrete", "conta errada", "consumo não gravou".
---

# Prompt e custo

Cada pergunta reenvia ferramentas + instrução + histórico. O que mantém isso barato é o cache; o que mantém o cliente
cobrado certo é o `POST /uso`.

## Estabilidade do cache

- Prefixo em cache = ferramentas + instrução, nesta ordem, com `cache_control` no fim de `INSTRUCAO`. O cache
  automático do topo (`cache_control={"type": "ephemeral"}`) cobre o histórico.
- **Qualquer byte que muda no prefixo invalida o cache de todo cliente.** Por isso a data de hoje
  (`contexto_de_hoje()`) e a obra em foco vão em `_conteudo_usuario`, na mensagem do usuário. Nunca em `INSTRUCAO`,
  nunca em docstring de ferramenta.
- Histórico só cresce: nunca reescreva mensagem antiga (`sessoes.py` recusa; quebraria cache e raciocínio).
- Teste-guarda: `test_instrucao_e_ferramentas_sao_estaveis_entre_conversas` (`tests/test_conversa.py`).

## Contexto longo

- `GESTAO_DE_CONTEXTO` (`conversa.py`): `clear_tool_uses_20250919` a partir de 150 mil tokens de entrada, mantém 8,
  limpa ao menos 30 mil. Conservador de propósito: cada limpeza quebra o cache do histórico dali em diante.
- `exclude_tools` = todos os `propor_*` (derivado de `NIVEIS`): o modelo tem de ver o que já propôs.
- O histórico guardado na API não é limpo; a limpeza é só no que o modelo vê.

## Lembretes deduplicados

- Aviso de "uma vez por conversa" tem chave (`f"sem_previsao:{obra_id}"`, datas futuras...). Use
  `eventos.com_lembretes(saida, {chave: texto})`: primeira vez vai o texto em `lembretes`, depois só a chave em
  `lembretes_ja_ditos`.
- Quem lembra é o código: `lembretes_do_historico(sessao.mensagens)` lê os `tool_result` guardados. Não peça ao
  modelo para lembrar.

## Contas

- O modelo não faz conta. Pronta da API (domínio) ou pela ferramenta `calcular` (`calculo.py`: Decimal, AST restrita,
  sem `eval`, "2.760" ambíguo recusado). `propor_pedido` calcula o total com `_conta_do_total` e devolve
  `conta_do_valor`.
- Conta nova de domínio (falta, saldo, atraso) vai para `canteiro_dominio.regras` no canteiro-api.

## Custo e consumo

- Toda resposta grava `POST /uso` na hora (`registrar` em `_volta`): tokens e `custo_microdolares` por `medir`
  (`precos.py`, Decimal, arredonda uma vez). Fallback de recusa conta cada iteração no preço do modelo que rodou.
- Corte no meio: `UsoParcial` soma os eventos do stream e grava o que já foi gasto, protegido com `asyncio.shield`.
  O `id` fixo da linha evita duplicar em retentativa.
- Modelo sem preço em `PRECOS`: o serviço não sobe (`dependencias_padrao`). Modelo de fallback fora da tabela grava no
  preço do pedido e loga a linha para reconciliar.
- Modo roteiro não grava uso (seria descontar crédito por resposta grátis).
- **Chave própria:** `GET /creditos` → `chave_propria: true` → `FonteChaveApi` busca a chave; sem ela, erro. Nunca cair
  na chave da plataforma (a plataforma pagaria o cliente). A chave não vai para log; o cliente próprio é fechado no fim.
- Créditos conferidos **antes** de qualquer chamada.

## Trocar modelo ou esforço

- `AGENTE_MODELO` + linha em `PRECOS` (com fonte e data no docstring). `ESFORCO_POR_ROTA` em `config.py`.
- Qualidade só se mede com modelo real e autorização (veja `testes-roteiro`).

## Checklist

- [ ] Nada dinâmico entrou em `INSTRUCAO`, nas docstrings ou nos esquemas.
- [ ] Toda chamada nova ao modelo confere créditos antes e grava `/uso` (com `rota` própria) antes de validar a saída.
- [ ] Preço novo tem fonte; `tests/test_precos.py` cobre.
- [ ] Lembrete novo usa `com_lembretes` com chave estável.
- [ ] `.venv/bin/pytest` verde sem rede.
