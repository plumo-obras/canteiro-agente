---
name: seguranca-ia
description: Use ao mexer em algo que leva texto de terceiros ao modelo (dados da obra, observação de pedido, legenda ou arquivo de comprovante), em autenticação/tokens (api_central.py, chaves.py, conversa.py _claims/_dono), em limites (tamanho, timeout, travas) ou em logs. Gatilhos: "prompt injection", "o agente obedeceu um texto do insumo", "vazou chave", "PII", "API caiu", "timeout", "duas abas", "revisão de segurança", e antes de publicar qualquer arquivo (o repositório é PÚBLICO).
---

# Segurança do agente

O agente age com o token de uma pessoa e lê texto que outras pessoas escreveram. O modelo é a parte que não dá para
confiar: a segurança fica no código e na API.

## Texto de dado é dado, não instrução

- Nome de insumo, fornecedor, observação, motivo, legenda e conteúdo de comprovante podem trazer "ignore as regras".
  A instrução (`instrucao.py`, seção Limites) e `INSTRUCAO_LEITURA` dizem isso; o código não depende disso:
  - id vindo do modelo passa por `_id()` (`api_central.py`): só `[A-Za-z0-9_-]{1,64}`, nada de `../interno`;
  - a única escrita é `POST /propostas`, e uma pessoa aprova (`tests/test_so_propoe.py`);
  - legenda do comprovante vai entre `<anotacao>` com `<` e `>` removidos e cortada em `MAX_LEGENDA`
    (`pedido_de_leitura`), para não fechar a tag;
  - a revisão (domínio) descarta etapa que não está na árvore da obra.
- Texto novo de terceiro indo ao modelo: delimite, limite o tamanho e garanta que nenhuma ação dependa só do modelo
  obedecer.

## Identidade e papel

- Dado de cliente só com o token do usuário. O token de serviço serve só para `POST /uso` e `PUT
  /interno/comprovantes/{id}/leitura`, sempre com `X-Canteiro-Usuario`.
- `_claims` lê o JWT **sem** conferir assinatura e só depois que a API aceitou o token em `/creditos`. Serve para
  identificar (dono da sessão, linha de uso), nunca para autorizar. Papel a API resolve no banco.
- Sessão alheia: `NaoEncontrada` com a mensagem da API, conferida **antes** da trava de "ocupada" (não revela que a
  conversa existe).

## Segredos e PII

- Repositório público: nada de chave, token, e-mail real, senha ou id de serviço do Render em arquivo, teste ou log.
  Testes usam valores falsos (`tests/falsos.py`).
- Chave própria: nunca em log nem em disco; erro da rota de segredo loga só o status (`chaves.py`). Cliente próprio é
  criado e fechado por conversa.
- Log de erro: tipo da exceção e status, não corpo de resposta nem conteúdo de comprovante. `ApiForaDoAr.detalhe` é só
  o nome do erro de rede.
- Memória de longo prazo (quando vier, `docs/desenho-agente.md` §5): nada de dado pessoal além de nome e papel.

## Limites e falhas

- Corpo: `texto` até 8.000, ids até 64 (`main.py`, mensagens em `_CAMPOS`).
- Volta: 240 s (`tempo_max_volta`), 12 passos, até 2 repetições de JSON ilegível. Comprovante: 120 s, uma repetição,
  imagem acima de 5 MB em base64 não é enviada.
- Uma volta por sessão (`_em_andamento`), um comprovante por vez (`_lendo`).
- `ApiForaDoAr` (rede ou 502/503/504): a ferramenta devolve frase ao modelo, `api.fora_do_ar` para a volta sem pagar
  outra chamada, a tela recebe `FORA_DO_AR`.
- Sem crédito, sem chave própria disponível ou chave recusada: erro em frase, nunca fallback para a chave da plataforma.

## Checklist

- [ ] Todo id vindo do modelo passa por `_id()`.
- [ ] Nenhum caminho de escrita fora de `POST /propostas`; `test_so_propoe.py` verde.
- [ ] Texto de terceiro delimitado, sanitizado e com tamanho máximo.
- [ ] Nada de segredo, corpo de resposta ou conteúdo de documento em log.
- [ ] Toda chamada externa nova tem timeout e erro em frase para a tela.
- [ ] `git diff` revisado procurando chave, e-mail, URL interna ou id de serviço antes do commit.
