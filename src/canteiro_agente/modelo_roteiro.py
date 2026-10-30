"""Servidor FALSO da API de mensagens da Anthropic, para teste de ponta a ponta sem gastar token.

    python -m canteiro_agente.modelo_roteiro --porta 8199
    ANTHROPIC_BASE_URL=http://localhost:8199 ANTHROPIC_API_KEY=falsa uvicorn canteiro_agente.main:app --port 8100

Responde `POST /v1/messages` em streaming (o mesmo SSE que o SDK lê da API real) seguindo um roteiro pela intenção
da última mensagem de texto do usuário. Os números da resposta saem do tool_result que o agente devolveu (o roteiro
lê e cita), nunca de constantes: se a API mudar o seed, a resposta muda junto.

- "bloco" / "2.760": consultar_estoque → historico_movimentos → texto com a conta e a quebra. Não propõe nada.
- "cimento": consultar_estoque → propor_pedido do que falta → texto curto.
- "aço": consultar_estoque → texto: o que está em cotação já cobre, falta 0, não precisa pedir.
- qualquer outra coisa: "não sei com segurança".

Sem obra em foco, começa por listar_obras e usa a primeira. Não é o modelo: não tem raciocínio, não valida chave e
não deve ser exposto fora da máquina de desenvolvimento.
"""
import argparse
import json
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import count
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from canteiro_dominio.tipos import fmt_qtd


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


_ids = count(1)


def ferramenta(nome: str, entrada: dict[str, Any], id_: str | None = None) -> dict[str, Any]:
    return {"type": "tool_use", "id": id_ or f"toolu_roteiro_{next(_ids)}", "name": nome, "input": entrada}


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
                partes.append(_sse("content_block_delta", {"index": i, "delta": {"type": "text_delta", "text": pedaco}}))
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
    if "bloco" in t or "2.760" in t:
        return "blocos"
    if "cimento" in t:
        return "cimento"
    if re.search(r"\baco\b", t):
        return "aco"
    return "desconhecido"


# ---------------------------------------------------------------- roteiro

NAO_SEI = "Não sei com segurança. Posso consultar estoque e pedidos de uma obra: me diga o que precisa."


def _qtd_para_ferramenta(milesimos: int) -> str:
    """Formato que propor_* aceita: sem separador de milhar, vírgula decimal ("34", "1,5")."""
    d = (Decimal(milesimos) / 1000).normalize()
    return f"{d:f}".replace(".", ",")


def _valor_do_texto(t: str) -> str | None:
    achou = re.search(r"(?:r\$\s*|por\s+)([\d.]+(?:,\d{1,2})?)", t, re.IGNORECASE)
    return achou.group(1) if achou else None


def _fornecedor_do_texto(t: str) -> str | None:
    achou = re.search(r"\b(?:na|no|da|do|com)\s+([A-ZÀ-Ý][\wÀ-ÿ]+(?:\s+[A-ZÀ-Ý][\wÀ-ÿ]+)*)", t)
    return achou.group(1) if achou else None


def roteiro(conv: Conversa) -> list[dict[str, Any]]:
    """Os blocos da próxima resposta do 'modelo'."""
    erro = next((p for p in conv.passos if p.erro), None)
    if erro:
        return [texto(f"Não consegui concluir: {erro.resultado}")]
    alvo = intencao(conv.texto)
    if alvo == "desconhecido":
        return [texto(NAO_SEI)]

    obra = conv.obra_id
    if obra is None:
        obras = conv.resultado("listar_obras")
        if obras is None:
            return [antes_de_ferramenta("Vou ver suas obras."), ferramenta("listar_obras", {})]
        if not obras:
            return [texto("Não encontrei nenhuma obra na sua conta.")]
        obra = obras[0]["obra_id"]

    busca = {"blocos": "bloco", "cimento": "cimento", "aco": "aço"}[alvo]
    estoque = conv.resultado("consultar_estoque")
    if estoque is None:
        return [antes_de_ferramenta("Vou conferir o almoxarifado."), ferramenta("consultar_estoque", {"obra_id": obra, "busca": busca})]
    if not estoque["insumos"]:
        return [texto(f"Não achei {busca} no almoxarifado desta obra.")]
    item = estoque["insumos"][0]
    conta, fmt = item["conta_milesimos"], item["conta_formatada"]
    # A conta vai num parágrafo só dela, como a ferramenta devolveu: é o que a pessoa confere.
    cabeca = (f"{item['nome']}, pelo almoxarifado:", item["linha_conta"])

    if alvo == "aco":
        if conta["falta"] == 0:
            return [texto(paragrafos(*cabeca, f"Os {fmt['cotando']} já estão em cotação e a falta é "
                                              f"**{fmt['falta']}**: **não precisa pedir**."))]
        return [texto(paragrafos(*cabeca, f"Falta **{fmt['falta']}**. Posso propor o pedido."))]

    if alvo == "cimento":
        proposta = conv.resultado("propor_pedido")
        if proposta is not None:
            return [texto(paragrafos(f"Deixei a proposta \"{proposta.get('titulo')}\" **aguardando aprovação**. "
                                     f"Nada foi comprado ainda.", "Conta:", item["linha_conta"]))]
        if conta["comprar"] == 0:
            return [texto(paragrafos(*cabeca, "Não precisa comprar."))]
        return [
            antes_de_ferramenta(paragrafos(*cabeca, f"Vou propor o pedido de **{fmt['comprar']}**.")),
            ferramenta("propor_pedido", {
                "obra_id": obra, "insumo_id": item["insumo_id"],
                "quantidade": _qtd_para_ferramenta(conta["comprar"]),
                "fornecedor": _fornecedor_do_texto(conv.texto) or "Votorantim",
                "valor": _valor_do_texto(conv.texto) or "1.360,00",
                "justificativa": f"repor a falta de {fmt['falta']} apontada pelo almoxarifado",
            }),
        ]

    # blocos: conta + quebra, sem propor nada.
    movimentos = conv.resultado("historico_movimentos")
    if movimentos is None:
        return [ferramenta("historico_movimentos", {"obra_id": obra, "insumo_id": item["insumo_id"], "limite": 50})]
    saidas = [m for m in movimentos if m.get("tipo") == "saida"]
    quebras = [m for m in saidas if "quebra" in _sem_acento(m.get("destino") or "")]
    if not quebras:
        return [texto(paragrafos(*cabeca, "Não há quebra registrada nas saídas."))]
    # Só no roteiro de teste: o agente de verdade não calcula índice (ver README).
    q, s = sum(m["quantidade_milesimos"] for m in quebras), sum(m["quantidade_milesimos"] for m in saidas)
    un = item["unidade"]
    return [texto(paragrafos(
        *cabeca,
        "Das saídas registradas:\n\n" + lista(
            f"saíram {fmt_qtd(s)} {un} no total",
            f"{fmt_qtd(q)} {un} foram quebra ({quebras[0]['destino']})",
            f"a quebra é **{round(100 * q / s)}%** do que saiu",
        ),
        "Não proponho ajuste: o saldo já desconta a quebra.",
    ))]


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
