"""Custo de cada chamada ao modelo, em microdólares inteiros (1 US$ = 1.000.000), para o livro de consumo.

Preço em US$ por milhão de tokens: multiplicar tokens pelo preço por milhão dá microdólares direto. A conta usa
Decimal e arredonda uma vez só no fim, para que a soma das linhas bata com a fatura.
Fonte: tabela pública da Anthropic (out/2026). Escrita de cache com TTL de 5 min = 1,25 × entrada.
Modelos de fallback entram aqui porque uma recusa resgatada é cobrada no preço do modelo que respondeu.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


@dataclass(frozen=True)
class Preco:
    entrada: Decimal
    saida: Decimal
    cache_leitura: Decimal
    cache_escrita: Decimal


def _p(entrada: str, saida: str, cache_leitura: str) -> Preco:
    e = Decimal(entrada)
    return Preco(e, Decimal(saida), Decimal(cache_leitura), e * Decimal("1.25"))


PRECOS: dict[str, Preco] = {
    "claude-opus-5-5": _p("4", "20", "0.20"),
    "claude-opus-5": _p("5", "25", "0.50"),
    "claude-opus-4-8": _p("5", "25", "0.50"),
    "claude-sonnet-5-5": _p("2", "10", "0.20"),
    "claude-sonnet-5": _p("2", "10", "0.20"),
    "claude-haiku-5-5": _p("0.10", "0.50", "0.01"),
    "claude-fable-5-1": _p("10", "50", "0.25"),
}


class ModeloSemPreco(KeyError):
    """Modelo fora da tabela: melhor falhar alto do que gravar consumo zerado."""


@dataclass(frozen=True)
class Tokens:
    entrada: int = 0
    saida: int = 0
    cache_leitura: int = 0
    cache_escrita: int = 0

    def __add__(self, o: "Tokens") -> "Tokens":
        return Tokens(
            self.entrada + o.entrada,
            self.saida + o.saida,
            self.cache_leitura + o.cache_leitura,
            self.cache_escrita + o.cache_escrita,
        )

    @property
    def total(self) -> int:
        return self.entrada + self.saida + self.cache_leitura + self.cache_escrita


def _custo_exato(modelo: str, t: Tokens) -> Decimal:
    try:
        p = PRECOS[modelo]
    except KeyError:
        raise ModeloSemPreco(f"Modelo sem preço na tabela: {modelo}") from None
    return (
        t.entrada * p.entrada
        + t.saida * p.saida
        + t.cache_leitura * p.cache_leitura
        + t.cache_escrita * p.cache_escrita
    )


def _arredonda(d: Decimal) -> int:
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def custo_microdolares(modelo: str, t: Tokens) -> int:
    return _arredonda(_custo_exato(modelo, t))


def _tokens_de(u: Any) -> Tokens:
    return Tokens(
        entrada=getattr(u, "input_tokens", 0) or 0,
        saida=getattr(u, "output_tokens", 0) or 0,
        cache_leitura=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_escrita=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


def medir(modelo_pedido: str, modelo_resposta: str | None, usage: Any) -> tuple[Tokens, int]:
    """Tokens e custo de uma resposta.

    Se houve fallback (uma recusa resgatada por outro modelo), `usage.iterations` traz uma linha por modelo que rodou,
    cada uma no seu preço. Sem fallback, vale o `usage` do topo no preço do modelo que respondeu.
    """
    iteracoes = getattr(usage, "iterations", None) or []
    if any(getattr(i, "type", None) == "fallback_message" for i in iteracoes):
        total, exato = Tokens(), Decimal(0)
        for i in iteracoes:
            if getattr(i, "type", None) not in ("message", "fallback_message"):
                continue
            t = _tokens_de(i)
            total += t
            exato += _custo_exato(getattr(i, "model", None) or modelo_pedido, t)
        return total, _arredonda(exato)
    t = _tokens_de(usage)
    return t, custo_microdolares(modelo_resposta or modelo_pedido, t)
