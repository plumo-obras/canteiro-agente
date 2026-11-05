"""A calculadora do agente: o modelo escreve a conta, o código faz.

No teste com o modelo real ele recusou "7 × 130" (a instrução proibia conta) e, em outra pergunta, fez sozinho o
"10 − 8 = −2 m³" do ajuste. Os dois erros têm a mesma causa: conta feita (ou recusada) de cabeça. Aqui a expressão é
lida por uma árvore sintática restrita (só números, + − × ÷ e parênteses), avaliada em Decimal e devolvida como a
conta pronta para citar ("7 m³ × R$ 130,00/m³ = R$ 910,00").

Nada de `eval`: o texto passa por um tokenizador que só aceita número, operador, parêntese e rótulo entre colchetes;
os números viram nomes `_0`, `_1`… antes do `ast.parse`, e o avaliador só conhece BinOp de + − × ÷, sinal e esses
nomes. Potência, chamada de função, atributo e qualquer outro nó são recusados.
"""
import ast
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, DivisionByZero, InvalidOperation, localcontext

from canteiro_dominio.tipos import fmt_qtd, fmt_reais

MAX_CARACTERES = 300
MAX_NUMEROS = 30
LIMITE = Decimal("1e15")            # acima disso não é conta de obra; é erro de digitação ou abuso

_TOKEN = re.compile(r"""
    (?P<espaco>\s+)
  | (?P<numero>\d+(?:[.,]\d+)*)
  | (?P<rotulo>\[[^\[\]]{0,40}\])
  | (?P<op>[+\-−*×x/÷:])
  | (?P<par>[()])
""", re.VERBOSE)
_OPS = {"+": "+", "-": "-", "−": "-", "*": "*", "×": "*", "x": "*", "/": "/", "÷": "/", ":": "/"}
_MOSTRA = {"+": "+", "-": "−", "*": "×", "/": "÷"}


class ErroConta(ValueError):
    """Mensagem pronta para o modelo (vira o erro da ferramenta)."""


@dataclass(frozen=True)
class Resultado:
    conta: str              # "7 m³ × R$ 130,00/m³ = R$ 910,00"
    resultado: str          # "R$ 910,00"
    valor: Decimal          # já arredondado (centavo em R$, milésimo no resto)


def _numero(texto: str) -> Decimal:
    """Número em formato brasileiro: vírgula decimal, ponto de milhar. "2.760" sozinho é ambíguo e é recusado, como
    no resto do sistema (adivinhar errado em estoque custa caro)."""
    if "," in texto:
        return Decimal(texto.replace(".", "").replace(",", "."))
    if "." in texto:
        partes = texto.split(".")
        if len(partes) > 2 or len(partes[-1]) == 3:
            raise ErroConta(f"'{texto}' é ambíguo: use vírgula para decimal (2.760,0 ou 2,76) ou escreva sem ponto.")
    return Decimal(texto)


def _dinheiro(rotulo: str | None) -> bool:
    return bool(rotulo) and rotulo.strip().upper().startswith("R$")


def _fmt(valor: Decimal, rotulo: str | None) -> str:
    """Número para mostrar: em reais vira "R$ 130,00" (o resto do rótulo, como "/m³", vem colado); senão "7 m³"."""
    sinal = "−" if valor < 0 else ""
    v = abs(valor)
    if _dinheiro(rotulo):
        centavos = int((v * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return sinal + fmt_reais(centavos) + rotulo.strip()[2:].strip()
    milesimos = int((v * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"{sinal}{fmt_qtd(milesimos)}" + (f" {rotulo.strip()}" if rotulo and rotulo.strip() else "")


def _tokens(expressao: str) -> list[tuple[str, str]]:
    if not expressao.strip():
        raise ErroConta("A conta está vazia.")
    if len(expressao) > MAX_CARACTERES:
        raise ErroConta(f"Conta longa demais (até {MAX_CARACTERES} caracteres): divida em partes.")
    saida, pos = [], 0
    while pos < len(expressao):
        m = _TOKEN.match(expressao, pos)
        if not m:
            raise ErroConta(f"Só números, + − × ÷, parênteses e [unidade] depois do número; não entendi "
                            f"'{expressao[pos:pos + 15]}'.")
        if m.lastgroup != "espaco":
            saida.append((m.lastgroup, m.group()))
        pos = m.end()
    return saida


def _avaliar(no: ast.AST, valores: dict[str, Decimal]) -> Decimal:
    match no:
        case ast.Expression(body=corpo):
            return _avaliar(corpo, valores)
        case ast.Name(id=nome) if nome in valores:
            return valores[nome]
        case ast.UnaryOp(op=ast.USub(), operand=o):
            return -_avaliar(o, valores)
        case ast.UnaryOp(op=ast.UAdd(), operand=o):
            return _avaliar(o, valores)
        case ast.BinOp(left=a, op=op, right=b) if isinstance(op, ast.Add | ast.Sub | ast.Mult | ast.Div):
            x, y = _avaliar(a, valores), _avaliar(b, valores)
            if isinstance(op, ast.Div) and y == 0:
                raise ErroConta("Divisão por zero.")
            r = x + y if isinstance(op, ast.Add) else x - y if isinstance(op, ast.Sub) else \
                x * y if isinstance(op, ast.Mult) else x / y
            if abs(r) > LIMITE:
                raise ErroConta("Resultado grande demais para uma conta de obra; confira os números.")
            return r
    raise ErroConta("Só números, + − × ÷ e parênteses.")


def calcular(expressao: str, unidade_resultado: str | None = None) -> Resultado:
    """Avalia "7 [m³] × 130,00 [R$/m³]" e devolve a conta formatada. `unidade_resultado` "R$" arredonda ao centavo
    (meio para cima); outra unidade, ao milésimo."""
    tokens = _tokens(expressao)
    valores: dict[str, Decimal] = {}
    python, mostra = [], []
    anterior = None
    for i, (tipo, texto) in enumerate(tokens):
        if tipo == "numero":
            if len(valores) >= MAX_NUMEROS:
                raise ErroConta(f"Números demais numa conta só (até {MAX_NUMEROS}).")
            nome = f"_{len(valores)}"
            valores[nome] = _numero(texto)
            rotulo = tokens[i + 1][1][1:-1] if i + 1 < len(tokens) and tokens[i + 1][0] == "rotulo" else None
            python.append(nome)
            mostra.append(_fmt(valores[nome], rotulo))
        elif tipo == "rotulo":
            if anterior != "numero":
                raise ErroConta("O rótulo entre colchetes vem logo depois de um número: 7 [m³].")
        elif tipo == "op":
            op = _OPS[texto]
            python.append(op)
            unario = anterior in (None, "op", "(")
            mostra.append(_MOSTRA[op] if unario else f" {_MOSTRA[op]} ")
        else:
            python.append(texto)
            mostra.append(texto)
        anterior = "(" if texto == "(" else tipo if tipo != "rotulo" else "numero"
    if not valores:
        raise ErroConta("A conta não tem número.")
    try:
        arvore = ast.parse(" ".join(python), mode="eval")
    except SyntaxError:
        raise ErroConta("A conta está mal escrita (parêntese ou operador sobrando).") from None
    with localcontext() as ctx:
        ctx.prec = 34
        try:
            bruto = _avaliar(arvore, valores)
        except (InvalidOperation, DivisionByZero):
            raise ErroConta("Divisão por zero.") from None
    dinheiro = _dinheiro(unidade_resultado)
    valor = bruto.quantize(Decimal("0.01") if dinheiro else Decimal("0.001"), rounding=ROUND_HALF_UP)
    resultado = _fmt(valor, unidade_resultado)
    return Resultado(conta=f"{''.join(mostra)} = {resultado}", resultado=resultado, valor=valor)
