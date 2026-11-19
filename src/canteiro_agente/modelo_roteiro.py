"""Servidor FALSO da API de mensagens da Anthropic, para teste de ponta a ponta sem gastar token.

    python -m canteiro_agente.modelo_roteiro --porta 8199
    ANTHROPIC_BASE_URL=http://localhost:8199 ANTHROPIC_API_KEY=falsa uvicorn canteiro_agente.main:app --port 8100

Responde `POST /v1/messages` em streaming (o mesmo SSE que o SDK lê da API real) seguindo um roteiro pela intenção
da última mensagem de texto do usuário. Os números da resposta saem do tool_result que o agente devolveu (o roteiro
lê e cita), nunca de constantes: se a API mudar o seed, a resposta muda junto.

- "como tá a obra" / "resumo" / "e aí": resumo_obra → resposta curta (uma frase, até cinco itens, o que falta
  cadastrar numa linha no fim).
- "quanto já gastei" / "orçamento" / "estourou" (com ou sem o nome de uma etapa): consultar_orcamento → o gasto e o
  que falta gastar, da etapa citada ou da obra, e as etapas que passaram do orçado.
- "imposto" / "INSS" / "tributo" / "IBS" / "CBS" / "CNO": consultar_tributos → as linhas da conta de cada tributo.
- "compra" / "pedido" / "estoque": nenhuma ferramenta; diz que o Plumo não faz compras e o que ele faz no lugar.
- qualquer outra coisa: "não sei com segurança".

Sem obra em foco, começa por listar_obras e usa a primeira. Não é o modelo: não tem raciocínio, não valida chave e
não deve ser exposto fora da máquina de desenvolvimento.
"""
import argparse
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from itertools import count
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

# ---------------------------------------------------------------- formato da API (SSE e JSON)

@dataclass
class Resposta:
    blocos: list[dict[str, Any]]
    stop_reason: str = "end_turn"
    modelo: str = "claude-opus-5-5"
    entrada: int = 100
    saida: int = 20
    cache_leitura: int = 0
    cache_escrita: int = 0


def texto(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def antes_de_ferramenta(t: str) -> dict[str, Any]:
    """Texto que precede uma chamada de ferramenta. Termina em linha em branco: na tela os blocos de texto da mesma
    resposta chegam como um fluxo só de deltas, e sem a quebra o "Vou conferir." colaria no parágrafo seguinte."""
    return texto(t.rstrip("\n") + "\n\n")


def paragrafos(*partes: str) -> str:
    """Markdown com parágrafos separados por linha em branco (uma quebra simples o Markdown junta numa linha só)."""
    return "\n\n".join(partes)


def lista(*itens: str) -> str:
    return "\n".join(f"- {i}" for i in itens)




def ferramenta(nome: str, entrada: dict[str, Any], id_: str | None = None) -> dict[str, Any]:
    # Único entre reinícios: o agente usa (sessão, id da chamada) como chave de idempotência da proposta, e um contador
    # que volta a 1 repetiria a chave de uma proposta antiga da mesma conversa.
    return {"type": "tool_use", "id": id_ or f"toolu_roteiro_{uuid.uuid4().hex[:16]}", "name": nome, "input": entrada}


def _uso(r: Resposta) -> dict[str, int]:
    return {"input_tokens": r.entrada, "output_tokens": r.saida, "cache_read_input_tokens": r.cache_leitura,
            "cache_creation_input_tokens": r.cache_escrita}


def _sse(tipo: str, dados: dict[str, Any]) -> str:
    return f"event: {tipo}\ndata: {json.dumps({'type': tipo, **dados}, ensure_ascii=False)}\n\n"


def eventos_sse(r: Resposta, n: int) -> str:
    """Uma resposta inteira como a sequência de eventos do streaming da API de mensagens."""
    uso = _uso(r)
    partes = [_sse("message_start", {"message": {
        "id": f"msg_roteiro_{n}", "type": "message", "role": "assistant", "model": r.modelo, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {**uso, "output_tokens": 1}}})]
    for i, b in enumerate(r.blocos):
        if b["type"] == "text":
            partes.append(_sse("content_block_start", {"index": i, "content_block": {"type": "text", "text": ""}}))
            meio = len(b["text"]) // 2
            for pedaco in (b["text"][:meio], b["text"][meio:]):
                partes.append(_sse("content_block_delta",
                                   {"index": i, "delta": {"type": "text_delta", "text": pedaco}}))
        else:
            partes.append(_sse("content_block_start", {"index": i, "content_block": {
                "type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}))
            partes.append(_sse("content_block_delta", {"index": i, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(b["input"], ensure_ascii=False)}}))
        partes.append(_sse("content_block_stop", {"index": i}))
    partes.append(_sse("message_delta", {"delta": {"stop_reason": r.stop_reason, "stop_sequence": None}, "usage": uso}))
    partes.append(_sse("message_stop", {}))
    return "".join(partes)


def mensagem_json(r: Resposta, n: int) -> dict[str, Any]:
    return {"id": f"msg_roteiro_{n}", "type": "message", "role": "assistant", "model": r.modelo,
            "content": r.blocos, "stop_reason": r.stop_reason, "stop_sequence": None, "usage": _uso(r)}


# ---------------------------------------------------------------- leitura da conversa

def _sem_acento(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t.casefold()) if unicodedata.category(c) != "Mn")


def _blocos(conteudo: Any) -> list[dict[str, Any]]:
    return [{"type": "text", "text": conteudo}] if isinstance(conteudo, str) else list(conteudo or [])


def _texto_resultado(bloco: dict[str, Any]) -> str:
    c = bloco.get("content")
    return c if isinstance(c, str) else "".join(p.get("text", "") for p in c or [])


@dataclass
class Passo:
    nome: str
    resultado: Any
    erro: bool


@dataclass
class Conversa:
    texto: str = ""                          # última mensagem de texto do usuário (sem o contexto da tela)
    obra_id: str | None = None
    passos: list[Passo] = field(default_factory=list)

    def resultado(self, nome: str) -> Any:
        return next((p.resultado for p in self.passos if p.nome == nome and not p.erro), None)


def ler_conversa(mensagens: list[dict[str, Any]]) -> Conversa:
    """Acha a última pergunta de texto do usuário e as ferramentas já rodadas depois dela."""
    inicio = 0
    for i, m in enumerate(mensagens):
        if m["role"] == "user" and any(b.get("type") == "text" for b in _blocos(m["content"])):
            inicio = i
    conv = Conversa()
    for b in _blocos(mensagens[inicio]["content"]):
        if b.get("type") != "text":
            continue
        achou = re.search(r"obra_id=([\w-]+)", b["text"])
        if b["text"].startswith("(Obra em foco") and achou:
            conv.obra_id = achou.group(1)
        elif b["text"].startswith("(Hoje é "):
            continue                         # contexto da data, não é a pergunta
        else:
            conv.texto += b["text"]
    nomes: dict[str, str] = {}
    for m in mensagens[inicio + 1:]:
        for b in _blocos(m["content"]):
            if b.get("type") == "tool_use":
                nomes[b["id"]] = b["name"]
            elif b.get("type") == "tool_result":
                bruto = _texto_resultado(b)
                try:
                    valor = json.loads(bruto)
                except ValueError:
                    valor = bruto
                conv.passos.append(Passo(nomes.get(b["tool_use_id"], "?"), valor, bool(b.get("is_error"))))
    return conv


def intencao(texto_usuario: str) -> str:
    t = _sem_acento(texto_usuario)
    if re.search(r"como (ta|esta|vai) a obra|\bresumo\b|^e ai\b", t.strip()):
        return "resumo"
    # antes do gasto: "quanto vou pagar de imposto?" é sobre tributo, não sobre o orçamento
    if re.search(r"\b(impostos?|inss|tributos?|ibs|cbs|cno|afericao|sero)\b", t):
        return "tributos"
    if re.search(r"\b(compr\w*|pedidos?|pede|pedir|estoque|almoxarifado|insumos?|cotac\w*)\b", t):
        return "compra"
    if re.search(r"gast|orcad|orcamento|estour|passou", t):
        return "orcamento"
    return "desconhecido"


# ---------------------------------------------------------------- roteiro

NAO_SEI = "Não sei com segurança. Posso ver o orçamento, os gastos e o cronograma de uma obra: me diga o que precisa."
SEM_COMPRA = paragrafos(
    "Eu não faço compras nem controlo estoque.",
    "Acompanho o **orçamento**, o **cronograma** e os **gastos** da obra. Pagou alguma coisa? Envie a foto do "
    "comprovante em Gastos que eu leio e sugiro a etapa.",
)


def roteiro(conv: Conversa) -> list[dict[str, Any]]:
    """Os blocos da próxima resposta do 'modelo'."""
    erro = next((p for p in conv.passos if p.erro), None)
    if erro:
        return [texto(f"Não consegui concluir: {erro.resultado}")]
    alvo = intencao(conv.texto)
    if alvo == "desconhecido":
        return [texto(NAO_SEI)]
    if alvo == "compra":
        return [texto(SEM_COMPRA)]

    obra = conv.obra_id
    if obra is None:
        obras = conv.resultado("listar_obras")
        if obras is None:
            return [antes_de_ferramenta("Vou ver suas obras."), ferramenta("listar_obras", {})]
        if not obras:
            return [texto("Não encontrei nenhuma obra na sua conta.")]
        obra = obras[0]["obra_id"]

    if alvo == "resumo":
        r = conv.resultado("resumo_obra")
        if r is None:
            return [ferramenta("resumo_obra", {"obra_id": obra})]
        return [texto(texto_resumo(r))]

    if alvo == "tributos":
        t = conv.resultado("consultar_tributos")
        if t is None:
            return [antes_de_ferramenta("Vou conferir os tributos da obra."),
                    ferramenta("consultar_tributos", {"obra_id": obra})]
        return [texto(texto_tributos(t))]

    o = conv.resultado("consultar_orcamento")
    if o is None:
        return [antes_de_ferramenta("Vou conferir o orçamento."), ferramenta("consultar_orcamento", {"obra_id": obra})]
    return [texto(texto_orcamento(o, conv.texto))]


def texto_orcamento(o: dict[str, Any], pergunta: str) -> str:
    """O gasto da etapa citada pelo nome, ou o da obra com as etapas que passaram do orçado. Números como vieram."""
    if not o["tem_orcamento"]:
        return "Esta obra ainda não tem orçamento: importe em Orçamento para eu acompanhar o gasto por etapa."
    t = _sem_acento(pergunta)
    etapa = next((e for e in o["etapas"]
                  if any(len(p) >= 5 and p in t for p in _sem_acento(e["descricao"]).split())), None)
    if etapa:
        fim = (f"**Passou do orçado**: falta gastar {etapa['falta_gastar']}." if etapa["estourou"]
               else f"Falta gastar **{etapa['falta_gastar']}**.")
        return paragrafos(f"{etapa['etapa']} {etapa['descricao']}: gasto {etapa['gasto']} de "
                          f"{etapa['orcado_atual']} orçados.", fim)
    partes = [f"A obra gastou **{o['gasto']}** de {o['orcado_atual']} orçados; falta gastar {o['falta_gastar']}."]
    estouradas = [e for e in o["etapas"] if e["estourou"]]
    if estouradas:
        partes.append(lista(*(f"{e['etapa']} {e['descricao']}: gasto {e['gasto']} de {e['orcado_atual']}"
                              for e in estouradas)))
    return paragrafos(*partes)


def _sem_pedido_de_repeticao(lembrete: str) -> str:
    # o lembrete traz uma instrução ao modelo ("Diga uma vez, no fim, …"); o roteiro mostra só o fato
    return lembrete.split(" Diga uma vez")[0]


def texto_resumo(r: dict[str, Any]) -> str:
    """O "como tá a obra?" no formato da instrução: a resposta numa frase, até cinco itens, o que falta cadastrar
    numa linha no fim (nunca na abertura). Os números são os do resumo, citados como vieram."""
    o, prazo, fin = r["obra"], r["prazo"], r["financeiro"]
    if prazo.get("cadastrado") is False:
        abertura = f"A {o['nome']} está {o['situacao']}."
    else:
        abertura = (f"A {o['nome']} está **{prazo['situacao_contra_atual'] or 'sem medição ainda'}**, com término "
                    f"em {prazo['termino_projetado'] or prazo['termino_contratual']}"
                    + (f" ({prazo['medido_x_previsto']})." if prazo.get("medido_x_previsto") else "."))
    itens = []
    if prazo.get("etapas_do_mes"):
        itens.append(f"Em {prazo['mes_corrente']}: " + "; ".join(prazo["etapas_do_mes"]))
    if prazo.get("etapas_atrasadas"):
        itens.append("Atrasada: " + "; ".join(prazo["etapas_atrasadas"]))
    if prazo.get("proximo_mes") and prazo["proximo_mes"]["etapas"]:
        # o mês seguinte inteiro, como veio (maior valor primeiro; cada etapa diz se começa ou continua)
        itens.append(f"Em {prazo['proximo_mes']['mes']}: " + "; ".join(prazo["proximo_mes"]["etapas"][:3]))
    elif prazo.get("proxima_a_comecar"):
        itens.append(f"Começa em {prazo['proxima_a_comecar']['mes']}: "
                     + "; ".join(prazo["proxima_a_comecar"]["etapas"]))
    if fin.get("tem_orcamento") is not False:
        itens.append(f"Financeiro: gasto {fin['gasto']} de {fin['orcado_atual']} "
                     f"(falta gastar {fin['falta_gastar']})")
    if fin.get("etapas_estouradas"):
        itens.append("Passou do orçado: " + "; ".join(fin["etapas_estouradas"]))
    if r["aprovacoes_pendentes"]["quantas"]:
        itens.append(f"{r['aprovacoes_pendentes']['quantas']} proposta(s) aguardando aprovação")
    partes = [abertura]
    if itens:
        partes.append(lista(*itens[:5]))
    if r.get("lembretes"):
        # só os novos: o que já foi dito nesta conversa vem em lembretes_ja_ditos e não se repete
        partes.append(" ".join(_sem_pedido_de_repeticao(x) for x in r["lembretes"].values()))
    return paragrafos(*partes)


def texto_tributos(t: dict[str, Any]) -> str:
    """INSS e IBS/CBS com as linhas da conta como vieram, e o aviso de estimativa no fim."""
    i, b = t["inss"], t["ibs_cbs"]
    if i["situacao"] == "calculado":
        partes = [f"O INSS da obra fica em **{i['valor_estimado']}** se a aferição fosse hoje:",
                  lista(*i["linhas_conta"])]
        if i.get("economia_se_tiver_nota_centavos"):
            partes.append(f"Com nota fiscal para os {i['mao_de_obra_paga_sem_nota']} de mão de obra pagos sem nota, "
                          f"o INSS cairia **{i['economia_se_tiver_nota']}**.")
    else:
        partes = ["INSS da obra: " + " ".join(i["motivos"])]
    if b["situacao"] == "calculado":
        partes += [f"IBS/CBS estimado a pagar: **{b['a_pagar']}**.", lista(*b["linhas_conta"]), b["transicao"]]
    else:
        partes.append("IBS/CBS: " + " ".join(b["motivos"]))
    if t["gastos_a_classificar"]["quantos"]:
        partes.append(f"{t['gastos_a_classificar']['quantos']} gasto(s) ({t['gastos_a_classificar']['valor']}) "
                      "ainda sem classificação fiscal: classifique em Tributos para entrarem na conta.")
    partes.append("É estimativa: confira com o contador antes de pagar ou declarar.")
    return paragrafos(*partes)


def _tokens(dado: Any) -> int:
    # Ordem de grandeza de um tokenizador real: ~4 caracteres por token.
    return max(1, len(json.dumps(dado, ensure_ascii=False)) // 4)


def responder(corpo: dict[str, Any]) -> Resposta:
    blocos = roteiro(ler_conversa(corpo["messages"]))
    prefixo = _tokens(corpo.get("tools", [])) + _tokens(corpo.get("system", ""))
    # Primeira volta escreve o prefixo (ferramentas + instrução) no cache; as seguintes leem.
    primeira = len(corpo["messages"]) == 1
    return Resposta(
        blocos=blocos,
        stop_reason="tool_use" if any(b["type"] == "tool_use" for b in blocos) else "end_turn",
        modelo=corpo.get("model", "claude-opus-5-5"),
        entrada=_tokens(corpo["messages"]),
        saida=_tokens(blocos) + 40,              # + raciocínio que o modelo real cobraria
        cache_escrita=prefixo if primeira else 0,
        cache_leitura=0 if primeira else prefixo,
    )


def criar_app() -> FastAPI:
    app = FastAPI(title="Modelo de roteiro (falso)")
    contador = count(1)

    @app.post("/v1/messages")
    async def mensagens(req: Request):
        corpo = await req.json()
        r, n = responder(corpo), next(contador)
        if corpo.get("stream"):
            return Response(eventos_sse(r, n), media_type="text/event-stream")
        return JSONResponse(mensagem_json(r, n))

    return app


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--porta", type=int, default=8199)
    p.add_argument("--host", default="127.0.0.1")
    a = p.parse_args()
    uvicorn.run(criar_app(), host=a.host, port=a.porta)


if __name__ == "__main__":
    main()
