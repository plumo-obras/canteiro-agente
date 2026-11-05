"""Uma volta de conversa: créditos → modelo com ferramentas (Tool Runner, em streaming) → eventos para a tela.

Ordem que importa:
1. `GET /creditos` antes de qualquer chamada ao modelo (sem saldo, nem começa).
2. Cada resposta do modelo grava `POST /uso` na hora, não no fim. O uso é somado pelos eventos do stream enquanto ele
   chega: se a conexão cair no meio (ou a volta estourar o tempo), o que já foi gasto vai para o livro de consumo
   mesmo assim, numa gravação protegida do cancelamento.
3. O histórico é espelhado aqui (mensagem do modelo, depois o resultado das ferramentas) e é gravado a cada rodada
   de ferramentas completa, não só no fim: uma proposta criada antes de a conexão cair já está no histórico, e a
   próxima pergunta não a propõe de novo. Nunca editamos mensagem antiga, o que invalidaria o cache e os blocos de
   raciocínio.

A volta roda numa tarefa própria e conversa com o SSE por uma fila. Assim o SSE manda "ping" enquanto espera, o
tempo máximo vale para a volta e não para o envio à tela, e o cancelamento que o servidor faz quando a pessoa fecha
a aba cai num ponto só, onde o consumo parcial é gravado.

Uma volta por sessão de cada vez (`_em_andamento`): duas abas na mesma conversa disputariam o mesmo `seq_inicial`, e
a perdedora levaria 409 em toda gravação, com a proposta dela fora do histórico. A trava é do processo, e basta
porque o agente roda numa instância só (Render). Com mais de uma instância ela deixa de valer entre elas; aí quem
segura a ordem é a API, que recusa com 409 o lote fora de sequência (`HistoricoDivergiu`), e a volta avisa que a
parte dela não foi salva.
"""
import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import httpx
from anthropic import AsyncAnthropic

from canteiro_dominio.tipos import novo_id

from .api_central import FORA_DO_AR, ApiCentral, ApiForaDoAr, ErroApi, registrar_uso
from .chaves import FonteChaveCliente
from .config import ESFORCO_POR_ROTA, Config
from .ferramentas import NIVEIS, Eventos, criar_ferramentas
from .instrucao import INSTRUCAO
from .precos import ModeloSemPreco, Tokens, medir
from .sessoes import HistoricoDivergiu, NaoEncontrada, RepositorioSessoes

log = logging.getLogger("canteiro_agente")

# Recusa do classificador de segurança é refeita no servidor em outro modelo, escolhido pela categoria da recusa.
BETA_FALLBACK = "server-side-fallback-2026-07-01"
BETA_CONTEXTO = "context-management-2025-06-27"
FUSO = ZoneInfo("America/Sao_Paulo")
MAX_REPETICOES_JSON = 2
TENTATIVAS_USO = 3

# Sessão longa cresce sem limite e cada pergunta reenvia tudo; o grosso são os resultados de ferramenta (JSON da
# API). A edição de contexto do servidor limpa os resultados antigos antes de o modelo ver, sem mexer no histórico
# guardado (que continua só crescendo). Conservadora de propósito: só dispara com contexto já grande, mantém as
# últimas chamadas e só limpa se valer a pena, porque cada limpeza quebra o cache do histórico dali em diante.
# As ferramentas que propõem ficam fora: o modelo precisa continuar vendo o que já propôs para não propor de novo.
GESTAO_DE_CONTEXTO: dict[str, Any] = {"edits": [{
    "type": "clear_tool_uses_20250919",
    "trigger": {"type": "input_tokens", "value": 150_000},
    "keep": {"type": "tool_uses", "value": 8},
    "clear_at_least": {"type": "input_tokens", "value": 30_000},
    "exclude_tools": sorted(nome for nome, nivel in NIVEIS.items() if nivel == "propoe"),
}]}

# Começo da mensagem do SDK quando não consegue ler o JSON de entrada de ferramenta que chega em streaming. É um
# ValueError genérico; o texto é o único jeito de não confundir com outro ValueError (que é bug e tem de aparecer).
_JSON_ILEGIVEL = "Unable to parse tool parameter JSON from model"

Evento = tuple[str, dict[str, Any]]
# Comentário SSE (nome começando com ":"): o navegador ignora, mas o tráfego mantém viva a conexão (proxy, função
# da Vercel) enquanto o modelo pensa ou uma ferramenta demora.
PING: Evento = (": ping", {})

AVISO_LIMITE_PASSOS = ("Precisei de mais passos do que o limite de uma resposta e parei antes de concluir. "
                       "Peça para eu continuar ou divida o pedido em partes menores.")
AVISO_TEMPO = ("A resposta demorou demais e foi interrompida. O que já foi feito ficou salvo; "
               "peça para eu continuar ou tente algo mais específico.")
AVISO_OCUPADA = "Esta conversa ainda está respondendo à pergunta anterior. Espere ela terminar e mande de novo."
AVISO_DIVERGIU = ("Esta conversa recebeu outra resposta ao mesmo tempo (outra aba?) e esta parte não foi salva. "
                  "Recarregue a conversa antes de continuar.")
AVISO_NAO_SALVOU = "Não consegui salvar esta parte da conversa; a próxima pergunta pode não lembrar dela."

# Sessões com uma volta em andamento neste processo. Conferir e marcar acontecem sem `await` no meio, então duas
# voltas no mesmo laço de eventos não passam juntas.
_em_andamento: set[str] = set()


@dataclass
class Dependencias:
    config: Config
    http_api: httpx.AsyncClient
    sessoes: RepositorioSessoes
    fonte_chave: FonteChaveCliente
    # Cliente da chave da plataforma (um só, reaproveita conexões). None se ANTHROPIC_API_KEY não estiver definida.
    cliente_plataforma: AsyncAnthropic | None
    # Cliente para a chave própria de um cliente: criado e fechado a cada conversa, a chave não fica em memória.
    fabrica_cliente_proprio: Callable[[str], AsyncAnthropic]


@dataclass
class PedidoConversa:
    texto: str
    sessao_id: str | None = None
    obra_id: str | None = None


def _erro(mensagem: str) -> Evento:
    return ("erro", {"mensagem": mensagem})


def _claims(autorizacao: str) -> dict[str, Any]:
    """Claims do JWT do usuário, lidas SEM conferir a assinatura. Só se usa depois que a API aceitou esse mesmo token
    em `GET /creditos`: quem conferiu foi ela. Servem para identificar (dono da sessão, linha de consumo), nunca para
    autorizar: papel e acesso a dados a API resolve no banco a cada requisição."""
    partes = autorizacao.removeprefix("Bearer ").strip().split(".")
    if len(partes) != 3:
        return {}
    try:
        carga = json.loads(base64.urlsafe_b64decode(partes[1] + "=" * (-len(partes[1]) % 4)))
    except ValueError:
        return {}
    return carga if isinstance(carga, dict) else {}


def _dono(autorizacao: str, creditos: dict[str, Any], claims: dict[str, Any]) -> str:
    """O token troca a cada 15 min, por isso não serve de dono sozinho; o hash dele é só o último recurso."""
    if creditos.get("usuario_id"):
        return str(creditos["usuario_id"])
    if claims.get("sub"):
        return str(claims["sub"])
    return "token:" + hashlib.sha256(autorizacao.encode()).hexdigest()


_DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def contexto_de_hoje(agora: datetime | None = None) -> str:
    """A data de hoje em Brasília, para o modelo separar passado de futuro (no teste ele repetia nota e aditivo
    datados no futuro sem estranhar). Vai na mensagem, nunca na instrução: mudaria o prefixo em cache todo dia."""
    hoje = (agora or datetime.now(FUSO)).astimezone(FUSO)
    return f"(Hoje é {_DIAS[hoje.weekday()]}, {hoje:%d/%m/%Y}, {hoje:%H:%M} no horário de Brasília.)"


def _conteudo_usuario(pedido: PedidoConversa) -> list[dict[str, Any]]:
    # A obra em foco e a data vão na mensagem, não na instrução: a instrução tem de ser idêntica em toda conversa.
    blocos = [{"type": "text", "text": contexto_de_hoje()}]
    if pedido.obra_id:
        blocos.append({"type": "text", "text": f"(Obra em foco na tela: obra_id={pedido.obra_id})"})
    blocos.append({"type": "text", "text": pedido.texto})
    return blocos


def _sem_pendencia(historico: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tira do fim uma chamada de ferramenta sem resultado (conversa interrompida): a API recusaria o histórico."""
    if historico and historico[-1]["role"] == "assistant":
        if any(b.get("type") == "tool_use" for b in historico[-1]["content"]):
            return historico[:-1]
    return historico


def _json_ilegivel(e: ValueError) -> bool:
    return str(e).startswith(_JSON_ILEGIVEL)


@dataclass
class UsoParcial:
    """Uso de uma resposta montado pelos eventos do stream, para quando a resposta não chega ao fim.

    Tem os mesmos nomes do `usage` da API, então `medir` lê este objeto como lê o da mensagem final. O
    `message_start` traz entrada e cache; o `message_delta` traz a saída (e repete o resto quando muda). Os números
    do delta são totais acumulados, por isso substituem em vez de somar.
    """
    modelo: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    iterations: Any = None
    texto: list[str] = field(default_factory=list)   # o que desta resposta já foi para a tela

    def ver(self, ev: Any) -> None:
        if ev.type == "message_start":
            self.modelo = ev.message.model
            self._copiar(ev.message.usage)
        elif ev.type == "message_delta":
            self._copiar(ev.usage)
        elif ev.type == "content_block_start" and getattr(ev.content_block, "type", None) == "fallback":
            # Recusa resgatada no meio do stream: quem responde dali em diante é o modelo do fallback.
            self.modelo = ev.content_block.to.model
        elif ev.type == "text":
            self.texto.append(ev.text)

    def _copiar(self, u: Any) -> None:
        for nome in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                     "iterations"):
            valor = getattr(u, nome, None)
            if valor is not None:
                setattr(self, nome, valor)

    @property
    def vazio(self) -> bool:
        return self.modelo is None


# Voltas e gravações de consumo em andamento. A referência forte impede o coletor de lixo de sumir com uma tarefa
# que já não tem quem a espere (o SSE foi cancelado); `aguardar_pendentes` deixa o desligamento e os testes
# esperarem por elas.
_pendentes: set[asyncio.Task[Any]] = set()


def _acompanhar(tarefa: asyncio.Task[Any]) -> asyncio.Task[Any]:
    _pendentes.add(tarefa)
    tarefa.add_done_callback(_pendentes.discard)
    return tarefa


async def aguardar_pendentes() -> None:
    while _pendentes:
        await asyncio.gather(*list(_pendentes), return_exceptions=True)


async def _gravar_uso(dep: Dependencias, autorizacao: str, uso: dict[str, Any]) -> None:
    if not dep.config.token_servico:
        log.error("TOKEN_SERVICO_AGENTE ausente: consumo não gravado %s", json.dumps(uso))
        return
    for tentativa in range(1, TENTATIVAS_USO + 1):
        try:
            await registrar_uso(dep.http_api, dep.config.api_url, dep.config.token_servico, autorizacao, uso)
            return
        except (ErroApi, httpx.HTTPError) as e:
            if tentativa == TENTATIVAS_USO:
                # O mesmo `id` permite reenviar depois sem duplicar; o log leva a linha inteira para reconciliar.
                log.error("Falha ao gravar consumo (%s): %s", e, json.dumps(uso))
                return
            await asyncio.sleep(0.2 * tentativa)


async def conversar(
    dep: Dependencias, autorizacao: str, pedido: PedidoConversa, rota: str = "conversa"
) -> AsyncIterator[Evento]:
    """Eventos da volta para o SSE, com `PING` nos silêncios. Fechar este gerador cancela a volta."""
    fila: asyncio.Queue[Evento | None] = asyncio.Queue()
    tarefa = _acompanhar(asyncio.create_task(_produzir(dep, autorizacao, pedido, rota, fila.put_nowait)))
    try:
        while True:
            try:
                evento = await asyncio.wait_for(fila.get(), dep.config.intervalo_ping)
            except TimeoutError:
                yield PING
                continue
            if evento is None:
                break
            yield evento
        await tarefa   # erro inesperado da volta sobe para o SSE, que avisa a tela
    finally:
        # A pessoa fechou a aba (o servidor cancela ou fecha este gerador): a volta para, e o cancelamento dela
        # grava o consumo parcial. Não esperamos aqui porque quem está sendo cancelado é este gerador.
        if not tarefa.done():
            tarefa.cancel()


async def _produzir(
    dep: Dependencias, autorizacao: str, pedido: PedidoConversa, rota: str, emitir: Callable[[Evento | None], None]
) -> None:
    estado: dict[str, Any] = {}
    try:
        try:
            async with asyncio.timeout(dep.config.tempo_max_volta) as prazo:
                await _volta(dep, autorizacao, pedido, rota, emitir, estado)
        except TimeoutError:
            if not prazo.expired():
                raise
            log.warning("Volta passou de %ss e foi interrompida (sessão %s)", dep.config.tempo_max_volta,
                        estado.get("sessao_id"))
            emitir(_erro(AVISO_TEMPO))
            if estado.get("sessao_id"):
                # Sem o `fim` a tela não guarda o sessao_id, e a próxima pergunta abriria outra conversa.
                emitir(("fim", {"sessao_id": estado["sessao_id"],
                                "uso": estado.get("uso", {"tokens": 0, "custo": 0})}))
    finally:
        emitir(None)


async def _volta(
    dep: Dependencias, autorizacao: str, pedido: PedidoConversa, rota: str,
    emitir: Callable[[Evento | None], None], estado: dict[str, Any],
) -> None:
    api = ApiCentral(dep.http_api, dep.config.api_url, autorizacao)

    # 1. Trava de crédito, antes de gastar qualquer token.
    try:
        creditos = await api.creditos()
    except ApiForaDoAr:
        emitir(_erro(FORA_DO_AR))
        return
    except ErroApi as e:
        if e.status == 402:
            emitir(_erro(e.mensagem
                         or "Os créditos de IA acabaram. Fale com o administrador da conta para recarregar."))
        elif e.status == 401:
            emitir(_erro("Sua sessão expirou. Entre de novo."))
        else:
            emitir(_erro("Não consegui consultar seus créditos agora. Tente de novo em instantes."))
        return
    except httpx.HTTPError:
        emitir(_erro(FORA_DO_AR))
        return

    # Modo roteiro (demonstração): ninguém fala com o modelo de verdade, nem quem tem chave própria.
    roteiro = dep.config.modelo_roteiro
    chave_propria = bool(creditos.get("chave_propria")) and not roteiro
    cliente_proprio: AsyncAnthropic | None = None
    if chave_propria:
        chave = await dep.fonte_chave.chave_do_cliente(autorizacao)
        if not chave:
            emitir(_erro("A chave própria da Anthropic desta conta ainda não está disponível para o agente."))
            return
        cliente = cliente_proprio = dep.fabrica_cliente_proprio(chave)
    elif dep.cliente_plataforma is None:
        emitir(_erro("O agente não está configurado neste ambiente."))
        return
    else:
        cliente = dep.cliente_plataforma

    claims = _claims(autorizacao)
    travada: str | None = None
    try:
        try:
            sessao = await dep.sessoes.abrir(
                pedido.sessao_id, dono=_dono(autorizacao, creditos, claims), autorizacao=autorizacao,
                obra_id=pedido.obra_id, titulo=pedido.texto[:80],
            )
        except NaoEncontrada as e:
            # a mensagem é a da API: "Conversa não encontrada." ou "Obra não encontrada.", sem revelar mais que isso
            emitir(_erro(str(e) or "Conversa não encontrada."))
            return
        except (ApiForaDoAr, httpx.HTTPError):
            emitir(_erro(FORA_DO_AR))
            return
        except ErroApi as e:
            emitir(_erro("Sua sessão expirou. Entre de novo." if e.status == 401
                         else "Não consegui abrir o histórico desta conversa. Tente de novo em instantes."))
            return
        except HistoricoDivergiu:
            emitir(_erro("Não consegui abrir o histórico desta conversa. Tente de novo em instantes."))
            return
        # Depois de abrir, e não antes: só o dono passa do `abrir`, então quem chuta o ID de uma conversa alheia
        # recebe "não encontrada", nunca "ainda está respondendo" (que diria que ela existe e está em uso).
        if sessao.id in _em_andamento:
            emitir(_erro(AVISO_OCUPADA))
            return
        _em_andamento.add(sessao.id)
        travada = sessao.id
        estado["sessao_id"] = sessao.id

        historico = sessao.mensagens + [{"role": "user", "content": _conteudo_usuario(pedido)}]
        eventos = Eventos(emitir=emitir, sessao_id=sessao.id)
        ferramentas = criar_ferramentas(api, eventos)
        divergiu = False
        total, custo_total = Tokens(), 0
        repeticoes_json = 0

        async def registrar(modelo_resposta: str | None, usage: Any) -> None:
            nonlocal total, custo_total
            if roteiro:
                return  # o roteiro não gasta token: gravar /uso descontaria créditos de verdade por respostas grátis
            try:
                tokens, custo = medir(dep.config.modelo, modelo_resposta, usage)
                sem_preco = False
            except ModeloSemPreco:
                # Com fallbacks="default" o servidor escolhe o modelo do resgate, e ele pode estar fora da tabela.
                # A resposta já foi paga: perder a linha é pior que um preço aproximado. Grava no preço do modelo
                # pedido e põe a linha inteira no log para acertar a diferença (UsoIA não tem campo de marcação, e
                # a API descartaria um campo a mais em silêncio).
                tokens, custo = medir(dep.config.modelo, modelo_resposta, usage, preco_de=dep.config.modelo)
                sem_preco = True
            total, custo_total = total + tokens, custo_total + custo
            estado["uso"] = {"tokens": total.total, "custo": custo_total}
            linha = {
                "id": novo_id(),
                # A API confere com o token do cabeçalho X-Canteiro-Usuario; o corpo segue o modelo UsoIA.
                "tenant_id": creditos.get("tenant_id") or claims.get("tid"),
                "usuario_id": creditos.get("usuario_id") or claims.get("sub"),
                "sessao_id": sessao.id,
                "obra_id": pedido.obra_id,
                "rota": rota,
                "modelo": modelo_resposta or dep.config.modelo,
                "tokens_entrada": tokens.entrada,
                "tokens_saida": tokens.saida,
                "tokens_cache_leitura": tokens.cache_leitura,
                "tokens_cache_escrita": tokens.cache_escrita,
                "custo_microdolares": custo,
                "chave_propria": chave_propria,
                "dia": datetime.now(FUSO).date().isoformat(),
            }
            if sem_preco:
                log.error("Modelo %s sem preço: consumo gravado no preço de %s; reconciliar: %s",
                          modelo_resposta, dep.config.modelo, json.dumps(linha))
            # A gravação roda numa tarefa própria e protegida: se a volta for cancelada enquanto espera, a linha
            # ainda chega à API. O `id` fixo garante que nada se grava duas vezes.
            await asyncio.shield(_acompanhar(asyncio.create_task(_gravar_uso(dep, autorizacao, linha))))

        async def registrar_parcial(parcial: UsoParcial | None) -> None:
            if parcial is not None and not parcial.vazio:
                await registrar(parcial.modelo, parcial)

        async def gravar_historico() -> bool:
            nonlocal divergiu
            novas = _sem_pendencia(historico)[len(sessao.mensagens):]
            if not novas:
                return True
            try:
                await dep.sessoes.acrescentar(sessao, novas, autorizacao=autorizacao)
                return True
            except HistoricoDivergiu as e:
                # outra instância gravou nesta sessão (a trava local não alcança outro processo)
                divergiu = True
                log.error("Histórico da sessão %s divergiu: %s", sessao.id, e)
                return False
            except (ErroApi, httpx.HTTPError) as e:
                log.error("Histórico da sessão %s não gravado: %s", sessao.id, e)
                return False

        # Resposta em andamento: o uso dela só existe aqui até a mensagem final chegar e ser registrada.
        atual: UsoParcial | None = None
        caiu = False   # a API central parou de responder no meio das ferramentas
        try:
            while True:
                runner = cliente.beta.messages.tool_runner(
                    model=dep.config.modelo,
                    max_tokens=dep.config.max_tokens,
                    max_iterations=dep.config.max_iteracoes,
                    # Um ponto de cache no fim da instrução cobre ferramentas + instrução (vêm antes, nessa ordem);
                    # o cache automático do topo cobre o histórico, que só cresce.
                    system=[{"type": "text", "text": INSTRUCAO, "cache_control": {"type": "ephemeral"}}],
                    cache_control={"type": "ephemeral"},
                    thinking={"type": "adaptive"},
                    output_config={"effort": ESFORCO_POR_ROTA.get(rota, "medium")},
                    tool_choice={"type": "auto"},
                    tools=ferramentas,
                    messages=historico,
                    context_management=GESTAO_DE_CONTEXTO,
                    betas=[BETA_FALLBACK, BETA_CONTEXTO],
                    fallbacks="default",
                    # O histórico é nosso (sessões portáveis), não uma thread guardada no servidor da Anthropic.
                    thread=False,
                    stream=True,
                )
                ultima_parada: str | None = None
                rodou_ferramentas = False
                try:
                    async for stream in runner:
                        atual = UsoParcial()
                        async for ev in stream:
                            atual.ver(ev)
                            if ev.type == "text":
                                emitir(("texto", {"delta": ev.text}))
                        msg = await stream.get_final_message()
                        atual = None   # daqui em diante quem registra é a mensagem final (sem contar duas vezes)
                        await registrar(msg.model, msg.usage)
                        eventos.nova_resposta(msg.content)
                        historico.append({"role": "assistant", "content": [b.to_dict() for b in msg.content]})
                        ultima_parada, rodou_ferramentas = msg.stop_reason, False
                        if msg.stop_reason == "refusal":
                            emitir(_erro("Não posso ajudar com esse pedido. Tente reformular."))
                            break
                        if msg.stop_reason == "max_tokens":
                            emitir(_erro("A resposta ficou longa demais e foi cortada. Peça algo mais específico."))
                            break
                        # Executa as ferramentas aqui (o runner reaproveita o resultado): cada uma anuncia o
                        # próprio evento `ferramenta` ao começar e o `proposta` ao criar.
                        resposta = await runner.generate_tool_call_response()
                        if resposta is not None:
                            historico.append(resposta)
                            rodou_ferramentas = True
                            if api.fora_do_ar:
                                # O SDK já entregou o erro como tool_result; seguir só pagaria outra resposta do
                                # modelo para tentar de novo contra a API caída. O aviso sai antes de tentar gravar
                                # (que provavelmente também falha, e pode demorar o tempo do timeout).
                                caiu = True
                                emitir(_erro(FORA_DO_AR))
                                break
                            # Rodada completa (chamada + resultado): grava já. Se a conexão cair depois de uma
                            # proposta, a próxima pergunta sabe que ela existe e não propõe de novo.
                            await gravar_historico()
                    else:
                        if ultima_parada == "tool_use" and rodou_ferramentas:
                            # O runner parou em max_iterations: há resultado de ferramenta que o modelo não leu.
                            emitir(_erro(AVISO_LIMITE_PASSOS))
                    break
                except ValueError as e:
                    if not _json_ilegivel(e):
                        raise
                    # JSON de chamada de ferramenta que o SDK não conseguiu ler (entrada em streaming). Não há
                    # tool_use_id para responder: refaz a volta a partir do histórico espelhado, com limite. A
                    # tentativa abortada também foi cobrada, e o texto dela já está na tela: a tela tira esse
                    # texto (evento `descartar`) antes de a nova tentativa escrever de novo.
                    parcial, atual = atual, None
                    await registrar_parcial(parcial)
                    if parcial is not None and parcial.texto:
                        emitir(("descartar", {"texto": "".join(parcial.texto)}))
                    repeticoes_json += 1
                    log.warning("Entrada de ferramenta ilegível; repetindo (%d)", repeticoes_json)
                    if repeticoes_json > MAX_REPETICOES_JSON:
                        emitir(_erro("Não consegui concluir agora. Tente de novo."))
                        break
                finally:
                    await runner.close()
        except asyncio.CancelledError:
            # Conexão caída ou tempo esgotado no meio de uma resposta: grava o que ela já gastou.
            parcial, atual = atual, None
            try:
                await registrar_parcial(parcial)
            except asyncio.CancelledError:
                pass   # cancelada de novo enquanto esperava: a gravação segue na tarefa protegida
            raise

        if not await gravar_historico() and not caiu:
            # A resposta já foi dada e o consumo já foi gravado; só o histórico desta volta se perde.
            emitir(_erro(AVISO_DIVERGIU if divergiu else AVISO_NAO_SALVOU))
        emitir(("fim", {"sessao_id": sessao.id, "uso": {"tokens": total.total, "custo": custo_total}}))

    except anthropic.AuthenticationError:
        emitir(_erro("A chave da Anthropic desta conta foi recusada." if chave_propria
                     else "O agente está com problema de configuração. Avise o suporte."))
    except anthropic.RateLimitError:
        emitir(_erro("Muitos pedidos ao mesmo tempo. Tente de novo em instantes."))
    except anthropic.APIStatusError as e:
        log.error("Erro do modelo: HTTP %s request_id=%s", e.status_code, getattr(e, "request_id", None))
        emitir(_erro("O assistente está indisponível agora. Tente de novo em instantes."))
    except anthropic.APIConnectionError:
        emitir(_erro("Não consegui falar com o assistente agora. Tente de novo em instantes."))
    finally:
        if travada is not None:
            _em_andamento.discard(travada)
        if cliente_proprio is not None:
            await cliente_proprio.close()
