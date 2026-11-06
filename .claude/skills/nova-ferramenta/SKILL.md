---
name: nova-ferramenta
description: Use ao criar ou mudar uma ferramenta do agente (leitura ou propor_*) em src/canteiro_agente/ferramentas.py, ao acrescentar um método no ApiCentral (api_central.py), ou quando a pessoa pedir "o agente precisa saber X", "deixa o agente propor Y", "nova ferramenta", "o agente não consegue ver Z". Também ao mexer em descrição, esquema ou nome de ferramenta existente.
---

# Nova ferramenta do agente

As ferramentas são o único jeito de o modelo ver dado ou pedir escrita. Erro aqui vira número inventado, proposta
duplicada ou escrita sem aprovação.

## Antes de codar

1. A rota existe na API? Confira `docs/contrato.md` do canteiro-api. Se não existe, a ferramenta começa lá, não aqui.
2. É **leitura** ou **proposta**? Escrita do agente é só `POST /propostas`. Não existe "ferramenta que grava".
3. A conta (falta, saldo, atraso, %) vem pronta da API (`canteiro_dominio.regras`)? Se não, ela vai para o domínio,
   não para a ferramenta nem para o modelo. Conta simples sobre números já lidos é `calcular`.

## Leitura

- Método novo em `ApiCentral` (`api_central.py`): só `GET`, por `self._req`, com todo segmento de URL vindo do modelo
  passado por `_id(...)` (impede `x/../../interno` com o token do usuário).
- Em `criar_ferramentas` (`ferramentas.py`): `@beta_async_tool(eager_input_streaming=True)`, `async def`, devolve
  `_json(...)`.
- Primeira linha: `eventos.ferramenta("nome", "resumo curto do que fez")` (é o que a tela mostra enquanto roda).
- `except ErroApi as e: raise _erro(e) from None`: o modelo recebe frase, nunca a exceção crua.
- Saída com bruto **e** formatado (`fmt_qtd`, `fmt_reais`, `fmt_data`, `fmt_mes`) e, quando houver, a linha de conta
  pronta (`linha_de_conta`). Datas para gente ("16/11/2026"), nunca ISO.
- Falta de cadastro ou de previsão vai como lembrete com chave estável: `eventos.com_lembretes(saida, {"chave:obra": TEXTO})`.

## Proposta

- Nome `propor_*`, nível `"propoe"` em `NIVEIS`.
- Valide tudo antes de propor: `_ler_qtd`, `_ler_reais`, `_data_iso(texto, eventos.hoje)`, existência do insumo/pedido
  (`_item_do_insumo`, `_pedido_da_obra`), transição pelo domínio (`validar_transicao`). Faltou dado: `ToolError` com a
  pergunta a fazer ("Falta o valor: pergunte o total...").
- Termine com `return await _propor({...}, extra)`: ele põe `chave_idempotencia = "{sessao_id}:{tool_use_id}"`, emite
  o evento `proposta` e devolve o aviso de "aguardando aprovação" ou de `ja_existia`.
- O corpo descreve o efeito para quem aprova: `titulo` legível ("Comprar 34 sc de Cimento CP-II de Votorantim por
  R$ 1.360,00"), `justificativa`, `conta`, `fonte`, `dados` em milésimos/centavos.
- Na lista final de `criar_ferramentas`, embrulhe com `_com_chamada(...)` (sem isso não há tool_use_id e a chave some).
- O tipo da proposta precisa existir na API (executor + desfazer). Novo tipo começa no canteiro-api.

## Descrição (é o que o modelo lê)

- Docstring: o que faz, **quando usar** e **quando não usar** (aponte a ferramenta certa). Em `Args:`, formato de cada
  campo com exemplo ("3200", "1,5", dd/mm/aaaa). Proposta diz "NÃO executa: cria uma proposta...".
- Bytes fixos: nada de data, nome de cliente ou valor dinâmico em nome, docstring ou esquema. Isso quebra o cache do
  prefixo para todo mundo (`test_instrucao_e_ferramentas_sao_estaveis_entre_conversas`).
- Se o fluxo muda o que o modelo deve fazer, ajuste também `instrucao.py` (veja a skill `voz-e-resposta`).

## Checklist

- [ ] Nome em `NIVEIS` com o nível certo; `LOCAIS` se não fala com a API.
- [ ] Entrada válida em `ENTRADAS` de `tests/test_so_propoe.py` (ferramenta nova sem entrada faz o teste falhar).
- [ ] Nome no conjunto `FERRAMENTAS` de `tests/test_conversa.py`.
- [ ] Rota dublada em `ApiFalsa` (`tests/falsos.py`) e teste em `tests/test_ferramentas.py` (chamada por
      `fs[nome].call(entrada)`), cobrindo erro 403/404 e entrada inválida.
- [ ] Proposta: teste de ponta a ponta com `Resposta([ferramenta(...)])` conferindo `chave_idempotencia` e que a
      única escrita é `POST /propostas`.
- [ ] Se for proposta: o nome entra sozinho em `exclude_tools` do `GESTAO_DE_CONTEXTO` (vem de `NIVEIS`); confira.
- [ ] Contagem "13 ferramentas" atualizada no README e em `docs/desenho-agente.md`.
- [ ] `.venv/bin/pytest` e `.venv/bin/ruff check src tests` verdes, sem gastar token.
