# Desenho do agente

Rascunho para revisão (04/11/2026). Define **quais trabalhos o agente faz**, como cada um começa, o que pode ler,
o que pode propor, quanto custa e como sabemos se ficou bom. Código vem depois deste documento.

## 1. Princípios (não mudam de trabalho para trabalho)

1. **O agente só propõe.** Toda escrita é uma proposta (`POST /propostas`) que uma pessoa aprova ou recusa; aprovar
   gera uma entrada no registro com desfazer. Há um teste estrutural (`tests/test_so_propoe.py`) que falha se surgir
   outra escrita.
2. **Dado vem das ferramentas; conta vem do domínio ou da calculadora.** O modelo não soma, não divide, não estima
   de cabeça. Falta, saldo, atraso e % medido chegam prontos da API (`canteiro_dominio`); o resto (7 × R$ 130, quanto
   falta medir) passa pela ferramenta `calcular` (Decimal, sem `eval`), que devolve a conta para citar. Se o número
   não existe, ele diz que ainda não está cadastrado.
3. **Texto dos dados é dado, não instrução.** Nome de insumo, observação de pedido, mensagem de WhatsApp e nota da
   memória podem conter tentativa de injeção; nada disso muda as regras.
4. **Cada execução tem dono e conta.** Toda chamada ao modelo grava `POST /uso` (tokens e custo) na construtora certa;
   com chave própria, a conta é dela e nós só medimos.
5. **Custo previsível.** Cada trabalho tem esforço, teto de passos, tempo máximo e orçamento de tokens definidos aqui.
6. **Portável.** O estado (conversa, memória, tarefas) mora na nossa API, não no fornecedor do modelo.

## 2. O que existe hoje

- **Conversa** (`POST /conversa`, SSE): Claude Opus 5.5 pelo Tool Runner do SDK da Anthropic, 13 ferramentas
  (8 de leitura, a calculadora `calcular` e 4 `propor_*`), pensamento adaptativo, esforço `medium`, até 12 passos e
  240 s por resposta. Pergunta aberta começa por `resumo_obra`; a data de hoje vai na mensagem do usuário.
- **Memória de curto prazo:** histórico em `sessoes`/`mensagens` na API, gravado a cada rodada; cache de prompt no
  prefixo fixo (ferramentas + instrução) e no histórico; limpeza de resultados antigos de ferramentas acima de 150 mil
  tokens (nunca dos `propor_*`).
- **Propostas:** `criar_pedido`, `registrar_entrega` (o "Registrar entrega" da tela), `avancar_pedido` (status e
  encerramento, sem entrada), `ajuste_estoque` (pela contagem). Sem duplicata: cada `propor_*` manda a chave
  `"{sessao_id}:{tool_use_id}"`, e a API devolve a proposta pendente igual (mesma obra, tipo e dados) em vez de criar
  outra (`ja_existia`).
- **Falhas:** API central fora do ar no meio das ferramentas para a volta (aviso na tela, consumo gravado, sem nova
  chamada ao modelo). Uma volta por sessão de cada vez (trava no processo: o agente roda numa instância só; com mais
  de uma, o 409 da API na gravação do histórico é o que segura a ordem).
- **Eventos de domínio:** a API grava na outbox `pedido.*`, `estoque.*`, `cronograma.salvo`, `medicao.criada`,
  `orcamento.*`, `proposta.*`, `registro.desfeito`, `obra.criada`. **Ninguém consome** (sem relay em produção).
- **Modo roteiro** (`MODELO_ROTEIRO=1`): responde sem modelo, para demonstração; não grava uso.

O agente hoje só reage a quem pergunta. Os trabalhos 2 a 4 abaixo são o que falta para ele "existir" no dia a dia.

## 3. Os trabalhos

Só o trabalho 1 é um agente aberto (o modelo escolhe os passos). Os outros são **fluxos**: o código decide o que
buscar e quando chamar o modelo; o modelo interpreta, prioriza, redige e propõe.

### T1. Conversa com o engenheiro (existe)

| | |
|---|---|
| Gatilho | Pergunta no chat |
| Entrada | Histórico da sessão + obra em foco + memória da obra (§5) |
| Ferramentas | As 10 atuais + `memoria` (§5) |
| Pode propor | Pedido, avanço de pedido, ajuste de estoque |
| Autonomia | Total sobre leitura; escrita só por proposta |
| Esforço / limites | `medium`, 12 passos, 240 s |
| Custo esperado | A medir com uso real (F5). Com cache, a pergunta típica deve pagar inteiro só o trecho novo |
| Como medimos | Conjunto de perguntas da obra de exemplo com resposta conferida (§7) |

Falta: memória de longo prazo, medição real de custo e uma avaliação.

### T2. Alertas da obra

| | |
|---|---|
| Gatilho | Evento da outbox (`pedido.status`, `pedido.recebimento`, `estoque.movimento`, `medicao.criada`, `cronograma.salvo`) e uma varredura diária |
| Quem decide se há alerta | **Regra no código** (domínio), não o modelo: entrega com previsão vencida, item que acaba antes da próxima entrega, etapa abaixo do previsto além da tolerância, recebimento parcial parado |
| Papel do modelo | Juntar alertas da mesma obra, explicar em uma frase com a conta da ferramenta e, quando cabe, montar a proposta (ex.: pedido do item que vai acabar) |
| Saída | Notificação na tela (novo) e, quando cabe, proposta |
| Autonomia | Não alerta duas vezes a mesma coisa; respeita as preferências da memória ("só me avise de atraso acima de 5%") |
| Esforço | `low` |
| Como medimos | Casos sintéticos por regra (dispara / não dispara) sem modelo; com modelo, a redação e a proposta |

### T3. Briefing diário

| | |
|---|---|
| Gatilho | Horário (ex.: 6h30 de cada dia útil, por obra ativa) |
| Entrada | Pacote montado pelo código: pendências de aprovação, entregas do dia e atrasadas, itens que acabam na semana, etapa da semana no cronograma, alertas abertos |
| Papel do modelo | Priorizar e escrever um resumo curto ("o que importa hoje"); propor o óbvio |
| Saída | Mensagem fixada no Início da obra (e WhatsApp, quando existir) |
| Esforço | `high` (já reservado em `ESFORCO_POR_ROTA`) |
| Custo | Uma chamada por obra por dia; candidato ao **Batch** da Anthropic (50% mais barato), porque não precisa de resposta imediata |
| Como medimos | Briefings de dias reais da obra de exemplo avaliados contra uma lista do que não pode faltar |

### T4. Ingestão (NF-e, foto, áudio, WhatsApp)

| | |
|---|---|
| Gatilho | Arquivo ou mensagem recebida |
| Fluxo | Extrair (XML da NF-e é determinístico; foto e áudio passam pelo modelo) → casar com pedido e insumo pelo domínio → propor |
| Saída | Proposta de recebimento (`avancar_pedido` com quantidades e NF) ou de saída do almoxarifado |
| Autonomia | Divergência de quantidade, preço ou item sem correspondência nunca vira proposta direta: vira pergunta ao responsável |
| Esforço | `medium` |
| Dependências | Canal de entrada (upload na tela primeiro; WhatsApp depois) e armazenamento de arquivos |
| Como medimos | NF-e e fotos reais anonimizadas, com o recebimento esperado |

## 4. Gatilhos e execução sem usuário

```
API ── outbox ──> relay ──> fila de tarefas ──> agente (rota alerta | briefing | ingestao) ──> POST /propostas
                    ▲                                                                       POST /notificacoes
cron (6h30) ────────┘
```

- **Relay da outbox:** um worker lê `outbox` não publicada em ordem, cria uma **tarefa** idempotente (id derivado do
  evento) e marca o evento como publicado. Começa como processo simples no Render; Pub/Sub entra quando o volume
  pedir. A interface fica a mesma.
- **Tarefa** guarda: construtora, obra, tipo, entrada, tentativas, resultado, custo. Falha repete com espera
  crescente; depois de N tentativas fica visível para nós.
- **Identidade sem usuário (decisão de segurança, §9):** a tarefa não tem o token de ninguém. Proposta: a API emite
  um **token de tarefa** curto, assinado, preso a uma construtora e uma obra, com escopo "ler + propor + notificar",
  e o agente usa esse token nas ferramentas como hoje usa o do usuário. O token de serviço continua servindo só para
  `/uso` e para emitir esses tokens. Nada de token de serviço com acesso a dado de cliente.
- **Créditos:** antes de rodar, a tarefa confere crédito como a conversa; sem crédito, não roda e registra o motivo.

## 5. Memória

### Curto prazo (a conversa)
Como hoje: histórico na API, cache de prompt, limpeza de contexto. Sem mudança.

### Longo prazo (entre conversas e tarefas)

**O que guardar:** preferências e fatos de trabalho que não estão nos dados da obra.
- Preferências: "mestre pede cimento às segundas", "não alertar atraso abaixo de 5%", "briefing sem valores".
- Fatos de relacionamento: "fornecedor X atrasou as 3 últimas entregas" (com a fonte), "a prefeitura mede no dia 25".
- Decisões: "trocamos brita 1 por brita 0 nas calçadas (aprovado em 12/11)".

**O que nunca guardar:**
- Números que a API já tem (saldo, preço, % medido): vêm sempre das ferramentas, nunca da memória.
- Dados pessoais além de nome e papel; segredos; conteúdo de documentos inteiros.

**Como funciona:**
- Ferramenta de memória da Anthropic (`memory_20250818`): o modelo lê, cria e edita notas curtas. O **armazenamento
  é nosso**: uma coleção `memorias` na API, por construtora e por obra (mais uma nota geral da construtora).
- No início de cada conversa e tarefa, o agente lê as notas da obra (são poucas e curtas; entram depois do prefixo
  em cache, para não quebrá-lo).
- Toda criação ou edição de nota gera entrada no registro com desfazer e aparece numa tela "O que o agente lembra"
  da obra, onde admin e master editam e apagam.
- Notas são **dados**: valem as mesmas regras contra injeção. Nota vinda de conversa com um usuário tem o autor.
- Limites: tamanho máximo por nota e por obra; nota sem uso há 180 dias é sugerida para limpeza.

## 6. Notificações (novo)

Os trabalhos 2 e 3 precisam de um lugar para falar sem ninguém perguntar.
- Coleção `notificacoes` na API (construtora, obra, tipo, texto, proposta ligada, lida, criada_em), com TTL.
- Na web: sino na barra e destaque no Início da obra; uma notificação pode abrir a proposta ligada.
- WhatsApp e e-mail ficam para depois, pelo mesmo registro.

## 7. Avaliação

Nada vai para produção sem um conjunto de casos e uma nota.
- **Base:** a obra de exemplo EMEI Santa Clara do Sul (dados reais) mais casos sintéticos para as regras.
- **Sem modelo (CI, grátis):** regras de alerta, montagem do pacote do briefing, casamento da NF-e, escopo do token
  de tarefa, "só propõe".
- **Com modelo (custa, roda sob autorização):** por trabalho, 20 a 50 casos com o resultado esperado; nota por
  critério (número citado da ferramenta, proposta certa, não inventou, tamanho). Guardamos a nota de cada versão da
  instrução para não regredir.

## 8. Ferramenta de construção (decidido)

- **Tool Runner do SDK da Anthropic**, no nosso serviço: já atende chave própria, uso por construtora, cache, limpeza
  de contexto e troca de modelo em recusa.
- **Sem LangGraph:** a memória dele é um checkpointer num banco, que já temos (`sessoes`/`mensagens`); o controle de
  fluxo dos trabalhos 2 a 4 é curto e fica mais claro em código. Reavaliar se algum fluxo virar uma sequência longa
  com espera por resposta humana no meio.
- **Managed Agents da Anthropic:** não agora (beta; sessões na organização de quem cria, o que conflita com a chave
  própria; ferramentas com token do usuário viram servidor sempre conectado ou MCP com credencial guardada; contêiner
  por sessão que não usamos). Reavaliar para trabalhos longos que precisem de sandbox (ex.: montar orçamento a partir
  de PDFs).

## 9. Decisões em aberto

1. **Token de tarefa** (§4): escopo, duração e quem pode revogar. Mexe em segurança: precisa do seu aceite.
2. **Modelo por trabalho:** tudo em Opus 5.5, ou um modelo menor (Haiku 5.5) para a redação dos alertas? Só com
   avaliação mostrando que a qualidade se mantém.
3. **Horário e destino do briefing:** hora, dias, quem recebe (engenheiro, mestre, diretor).
4. **Memória:** quem pode ver e apagar (proposta: admin e master), e se o viewer vê.
5. **Primeiro canal de ingestão:** upload de NF-e na tela antes do WhatsApp?
6. **Teto de gasto por construtora por mês** para os trabalhos automáticos.

## 10. Ordem proposta

1. **F5 — conversa com o modelo real:** medir custo e qualidade de T1 com a obra de exemplo (depende da chave da
   Anthropic em `~/.config/canteiro/anthropic.env`).
2. **Avaliação de T1** e ajuste da instrução.
3. **Memória de longo prazo** (§5) em T1.
4. **Relay da outbox + tarefas + token de tarefa** (§4), com uma regra de alerta só (entrega atrasada) e as
   notificações (§6).
5. **Briefing diário** (T3).
6. **Demais alertas** (T2).
7. **Ingestão de NF-e por upload** (T4), depois foto e áudio, depois WhatsApp.
