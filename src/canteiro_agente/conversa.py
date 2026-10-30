"""Uma volta de conversa: créditos → modelo com ferramentas (Tool Runner, em streaming) → eventos para a tela.

Ordem que importa:
1. `GET /creditos` antes de qualquer chamada ao modelo (sem saldo, nem começa).
2. Cada resposta do modelo grava `POST /uso` na hora, não no fim: se a conexão cair no meio, o que já foi gasto
   já está no livro de consumo.
3. O histórico é espelhado aqui (mensagem do modelo, depois o resultado das ferramentas) e só cresce no fim; nunca
   editamos mensagem antiga, o que manteria inválidos o cache e os blocos de raciocínio.
"""
import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import httpx
from anthropic import AsyncAnthropic

from canteiro_dominio.tipos import novo_id

from .api_central import ApiCentral, ErroApi, registrar_uso
from .chaves import FonteChaveCliente
from .config import ESFORCO_POR_ROTA, Config
from .ferramentas import Eventos, criar_ferramentas
from .instrucao import INSTRUCAO
from .precos import Tokens, medir
from .sessoes import HistoricoDivergiu, RepositorioSessoes, SessaoDeOutro

log = logging.getLogger("canteiro_agente")

# Recusa do classificador de segurança é refeita no servidor em outro modelo, escolhido pela categoria da recusa.
BETA_FALLBACK = "server-side-fallback-2026-07-01"
FUSO = ZoneInfo("America/Sao_Paulo")
MAX_REPETICOES_JSON = 2
TENTATIVAS_USO = 3

Evento = tuple[str, dict[str, Any]]


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


def _conteudo_usuario(pedido: PedidoConversa) -> list[dict[str, Any]]:
    # A obra em foco vai na mensagem, não na instrução: a instrução tem de ser idêntica em toda conversa (cache).
    blocos = []
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
    api = ApiCentral(dep.http_api, dep.config.api_url, autorizacao)

    # 1. Trava de crédito, antes de gastar qualquer token.
    try:
        creditos = await api.creditos()
    except ErroApi as e:
        if e.status == 402:
            yield _erro(e.mensagem or "Os créditos de IA acabaram. Fale com o administrador da conta para recarregar.")
        elif e.status == 401:
            yield _erro("Sua sessão expirou. Entre de novo.")
        else:
            yield _erro("Não consegui consultar seus créditos agora. Tente de novo em instantes.")
        return
    except httpx.HTTPError:
        yield _erro("O sistema está fora do ar agora. Tente de novo em instantes.")
        return

    chave_propria = bool(creditos.get("chave_propria"))
    cliente_proprio: AsyncAnthropic | None = None
    if chave_propria:
        chave = await dep.fonte_chave.chave_do_cliente(autorizacao)
        if not chave:
            yield _erro("A chave própria da Anthropic desta conta ainda não está disponível para o agente.")
            return
        cliente = cliente_proprio = dep.fabrica_cliente_proprio(chave)
    elif dep.cliente_plataforma is None:
        yield _erro("O agente não está configurado neste ambiente.")
        return
    else:
        cliente = dep.cliente_plataforma

    claims = _claims(autorizacao)
    try:
        try:
            sessao = await dep.sessoes.abrir(
                pedido.sessao_id, dono=_dono(autorizacao, creditos, claims), autorizacao=autorizacao,
                obra_id=pedido.obra_id, titulo=pedido.texto[:80],
            )
        except SessaoDeOutro as e:
            yield _erro(str(e))
            return
        except (ErroApi, httpx.HTTPError):
            yield _erro("Não consegui abrir o histórico desta conversa. Tente de novo em instantes.")
            return

        historico = sessao.mensagens + [{"role": "user", "content": _conteudo_usuario(pedido)}]
        eventos = Eventos()
        ferramentas = criar_ferramentas(api, eventos)
        total, custo_total = Tokens(), 0
        repeticoes_json = 0

        async def registrar(msg: Any) -> None:
            nonlocal total, custo_total
            tokens, custo = medir(dep.config.modelo, msg.model, msg.usage)
            total, custo_total = total + tokens, custo_total + custo
            await _gravar_uso(dep, autorizacao, {
                "id": novo_id(),
                # A API confere com o token do cabeçalho X-Canteiro-Usuario; o corpo segue o modelo UsoIA.
                "tenant_id": creditos.get("tenant_id") or claims.get("tid"),
                "usuario_id": creditos.get("usuario_id") or claims.get("sub"),
                "sessao_id": sessao.id,
                "obra_id": pedido.obra_id,
                "rota": rota,
                "modelo": msg.model or dep.config.modelo,
                "tokens_entrada": tokens.entrada,
                "tokens_saida": tokens.saida,
                "tokens_cache_leitura": tokens.cache_leitura,
                "tokens_cache_escrita": tokens.cache_escrita,
                "custo_microdolares": custo,
                "chave_propria": chave_propria,
                "dia": datetime.now(FUSO).date().isoformat(),
            })

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
                betas=[BETA_FALLBACK],
                fallbacks="default",
                # O histórico é nosso (sessões portáveis), não uma thread guardada no servidor da Anthropic.
                thread=False,
                stream=True,
            )
            try:
                async for stream in runner:
                    async for ev in stream:
                        if ev.type == "text":
                            yield ("texto", {"delta": ev.text})
                    msg = await stream.get_final_message()
                    await registrar(msg)
                    historico.append({"role": "assistant", "content": [b.to_dict() for b in msg.content]})
                    if msg.stop_reason == "refusal":
                        yield _erro("Não posso ajudar com esse pedido. Tente reformular.")
                        break
                    if msg.stop_reason == "max_tokens":
                        yield _erro("A resposta ficou longa demais e foi cortada. Peça algo mais específico.")
                        break
                    # Executa as ferramentas aqui (o runner reaproveita o resultado) para os eventos saírem na ordem.
                    resposta = await runner.generate_tool_call_response()
                    for evento in eventos.esvaziar():
                        yield evento
                    if resposta is not None:
                        historico.append(resposta)
                break
            except ValueError:
                # JSON de chamada de ferramenta que o SDK não conseguiu ler (entrada em streaming). Não há
                # tool_use_id para responder: refaz a volta a partir do histórico espelhado, com limite.
                repeticoes_json += 1
                log.warning("Entrada de ferramenta ilegível; repetindo (%d)", repeticoes_json)
                if repeticoes_json > MAX_REPETICOES_JSON:
                    yield _erro("Não consegui concluir agora. Tente de novo.")
                    break
            finally:
                await runner.close()

        novas = _sem_pendencia(historico)[len(sessao.mensagens):]
        try:
            await dep.sessoes.acrescentar(sessao, novas, autorizacao=autorizacao)
        except (HistoricoDivergiu, ErroApi, httpx.HTTPError) as e:
            # A resposta já foi dada e o consumo já foi gravado; só o histórico desta volta se perde.
            log.error("Histórico da sessão %s não gravado: %s", sessao.id, e)
            yield _erro("Não consegui salvar esta parte da conversa; a próxima pergunta pode não lembrar dela.")
        yield ("fim", {"sessao_id": sessao.id, "uso": {"tokens": total.total, "custo": custo_total}})

    except anthropic.AuthenticationError:
        yield _erro("A chave da Anthropic desta conta foi recusada." if chave_propria
                    else "O agente está com problema de configuração. Avise o suporte.")
    except anthropic.RateLimitError:
        yield _erro("Muitos pedidos ao mesmo tempo. Tente de novo em instantes.")
    except anthropic.APIStatusError as e:
        log.error("Erro do modelo: HTTP %s request_id=%s", e.status_code, getattr(e, "request_id", None))
        yield _erro("O assistente está indisponível agora. Tente de novo em instantes.")
    except anthropic.APIConnectionError:
        yield _erro("Não consegui falar com o assistente agora. Tente de novo em instantes.")
    finally:
        if cliente_proprio is not None:
            await cliente_proprio.close()
