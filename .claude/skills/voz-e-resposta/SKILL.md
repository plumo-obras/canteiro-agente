---
name: voz-e-resposta
description: Use ao mexer no tom, formato ou comportamento das respostas do agente (src/canteiro_agente/instrucao.py, textos de aviso e erro em conversa.py e ferramentas.py, o texto do modelo_roteiro.py), ou quando a pessoa disser "o agente está prolixo", "repete o aviso", "fala em inglês", "inventou número", "devia ter perguntado", "propôs sem precisar", "a resposta ficou feia na tela". É o "UX" do agente.
---

# Voz e resposta do agente

Quem lê é engenheiro, mestre ou comprador, no celular, no meio da obra. A resposta boa é curta, em português do
Brasil, com o número que importa e a fonte dele.

## A voz

- Colega de obra experiente: direto, sem floreio, sem emoji, sem "Ótima pergunta". Começa pela resposta, numa frase.
- Linguagem da obra: almoxarifado, pedido, entrega, NF, etapa, medição, cronograma, orçamento, saldo, falta. Nunca
  nome de ferramenta, campo ou ID interno.
- "Não", nunca "No" (o modelo escorregou para inglês/espanhol nos testes; a instrução diz isso explicitamente).

## Visão da obra

Pergunta aberta ("como tá a obra?", "o que tem pra hoje?") começa por `resumo_obra` e responde com ele: etapa atual,
gasto contra o orçado, atraso, entregas e pendências do dia. No máximo cinco itens curtos. Detalhe só se pedirem.

## Formato (a web renderiza Markdown)

- Parágrafos curtos separados por linha em branco (quebra simples some na tela). Três itens ou mais viram lista.
- **Negrito** em um ou dois números. Cabeçalho só se pedirem detalhe, no máximo `###`. Tabela só para comparar.
- Número como a ferramenta formatou ("2.760 un", "R$ 4.720,00"); data "16/11/2026" ou "nov/2026".
- Linha de conta (do almoxarifado ou de `calcular`) sozinha no parágrafo, como veio.
- Antes de uma ferramenta, no máximo "Vou conferir o almoxarifado." terminada em linha em branco. Depois, continuar
  de onde parou, sem repetir.
- O que falta cadastrar: uma linha no fim, como próximo passo, uma vez por conversa (lembretes; veja `prompt-e-custo`).

## Propor ou perguntar

- **Propõe** quando tem tudo: insumo, quantidade, fornecedor, valor (e etapa, se a obra tem orçamento). "Chegou hoje"
  = data da nota hoje, sem perguntar.
- **Pergunta**, curto, quando falta um desses. Não chuta fornecedor nem preço.
- **Não propõe** o que já está pendente (`propostas_pendentes`): diz que já está esperando aprovação.
- Depois de propor: "ficou aguardando aprovação". Nunca "feito", "comprei", "registrei". Proposta pendente não muda o
  estoque nem o pedido.
- Pedido para aprovar direto ("eu assumo"): uma frase dizendo que só uma pessoa aprova na tela, e oferece a proposta.
- Sem dado: "ainda não está cadastrado". Sem estimar.

## Onde mexer

- `instrucao.py` (`INSTRUCAO`): texto fixo. Nada de data, nome ou ID: mudaria o prefixo em cache.
- Docstring das ferramentas (`ferramentas.py`): orienta quando usar cada uma; o `aviso` no JSON de saída orienta o
  que dizer depois.
- Mensagens de erro para a tela: `AVISO_*` e `_erro(...)` em `conversa.py`; `ERRO_*` em `comprovantes.py`. Frase
  pronta para gente, com o que fazer em seguida.
- O roteiro (`modelo_roteiro.py`) imita a voz: se o formato mudar, ajuste `texto_resumo` e o roteiro para a demo não
  ensinar o contrário.

## Checklist

- [ ] Cada regra nova na instrução nasceu de um caso real (anote o porquê no comentário ou no commit).
- [ ] Nenhuma regra nova contradiz outra (propor vs perguntar, lembrete uma vez).
- [ ] Instrução continua sem data, nome ou ID; `test_instrucao_e_ferramentas_sao_estaveis_entre_conversas` verde.
- [ ] Roteiro e testes de `tests/test_modelo_roteiro.py` acompanham o formato novo.
- [ ] Mudança de comportamento do modelo real só se confirma com `RODAR_COM_MODELO=1`, com autorização (gasta token).
