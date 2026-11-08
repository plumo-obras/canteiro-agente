"""Garantia estrutural de "o agente só propõe": a única escrita que o agente alcança na API é `POST /propostas`.

Os outros testes provam o comportamento de cada ferramenta; este falha quando alguém acrescenta um caminho de escrita
novo (um método não-GET no `ApiCentral`, uma ferramenta que escreve fora de proposta, ou uma ferramenta que fala HTTP
por fora do `ApiCentral`), mesmo que o teste daquela ferramenta passe.
"""
import ast
import inspect
from pathlib import Path

import httpx
import pytest

from canteiro_agente import ferramentas as mod_ferramentas
from canteiro_agente.api_central import ApiCentral
from canteiro_agente.ferramentas import LOCAIS, NIVEIS, Eventos, criar_ferramentas

from falsos import ACO, BLOCO, CIMENTO, OBRA, OBRA_EMEI, PED_ACO, PED_BRITA, TOKEN_USUARIO, ApiFalsa

ESCRITA_PERMITIDA = {("POST", "/propostas")}

# Entrada válida para cada ferramenta: a chamada chega até a API (não para numa validação local), então o que ela
# alcança de fato é o que se registra. Ferramenta nova sem entrada aqui faz o teste falhar de propósito.
ENTRADAS: dict[str, dict] = {
    "listar_obras": {},
    "resumo_obra": {"obra_id": OBRA_EMEI},
    "consultar_estoque": {"obra_id": OBRA, "busca": "aço"},
    "historico_movimentos": {"obra_id": OBRA, "insumo_id": BLOCO},
    "listar_pedidos": {"obra_id": OBRA},
    "consultar_orcamento": {"obra_id": OBRA_EMEI},
    "consultar_cronograma": {"obra_id": OBRA_EMEI},
    "listar_fornecedores": {},
    "consultar_tributos": {"obra_id": OBRA},
    "calcular": {"expressao": "7 [m³] × 130 [R$/m³]", "unidade_resultado": "R$"},
    "propor_pedido": {"obra_id": OBRA, "insumo_id": CIMENTO, "quantidade": "34", "fornecedor": "Votorantim",
                      "valor": "1292", "justificativa": "repor a falta"},
    "propor_avanco_pedido": {"obra_id": OBRA, "pedido_id": PED_ACO, "para": "recebido",
                             "justificativa": "o caminhão chegou"},
    "propor_entrega": {"obra_id": OBRA_EMEI, "pedido_id": PED_BRITA, "linhas": [{"indice": 0, "quantidade": "7"}],
                       "justificativa": "chegou o resto", "nf_numero": "4410", "nf_valor": "910,00"},
    "propor_ajuste_estoque": {"obra_id": OBRA, "insumo_id": ACO, "quantidade": "-190", "motivo": "quebra"},
}


def _metodos_publicos() -> list[str]:
    return [n for n, f in inspect.getmembers(ApiCentral, inspect.iscoroutinefunction) if not n.startswith("_")]


def _argumento(p: inspect.Parameter):
    if p.default is not inspect.Parameter.empty:
        return p.default
    texto = str(p.annotation)
    return 1 if texto == "<class 'int'>" else {} if texto.startswith("dict") else "id-1"


async def test_api_central_so_escreve_em_propostas():
    """Chama todo método público do ApiCentral e registra o que chega ao transporte."""
    feitas: list[tuple[str, str]] = []

    def responder(req: httpx.Request) -> httpx.Response:
        feitas.append((req.method, req.url.path))
        return httpx.Response(200, json={})

    transporte = httpx.MockTransport(responder)
    async with httpx.AsyncClient(transport=transporte) as http:
        api = ApiCentral(http, "http://api.teste", TOKEN_USUARIO)
        metodos = _metodos_publicos()
        assert "criar_proposta" in metodos
        for nome in metodos:
            antes = len(feitas)
            assinatura = inspect.signature(getattr(ApiCentral, nome))
            args = [_argumento(p) for p in list(assinatura.parameters.values())[1:]]
            await getattr(api, nome)(*args)
            assert len(feitas) > antes, f"ApiCentral.{nome} não fez requisição: o teste não está vendo o que ela faz"
    escritas = {(m, c) for m, c in feitas if m != "GET"}
    assert escritas == ESCRITA_PERMITIDA, f"escrita fora de proposta: {escritas - ESCRITA_PERMITIDA}"


def test_api_central_nao_tem_outro_caminho_de_requisicao():
    """Método que chame o cliente HTTP direto (sem `_req`) escaparia do teste acima se ninguém o chamasse com os
    argumentos certos; aqui o próprio código-fonte da classe é conferido."""
    fonte = inspect.getsource(ApiCentral)
    arvore = ast.parse(fonte)
    usos_http = [n for n in ast.walk(arvore)
                 if isinstance(n, ast.Attribute) and n.attr == "_http" and isinstance(n.ctx, ast.Load)]
    # Um uso só: dentro de `_resp`, que todo método público atravessa (direto ou por `_req`).
    assert len(usos_http) == 1
    # o `_req` repassa ao `_resp` o método que recebeu: o que se confere são as chamadas dos métodos públicos
    req = next(n for n in ast.walk(arvore) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_req")
    de_dentro = {id(n) for n in ast.walk(req)}
    chamadas_req = [n for n in ast.walk(arvore) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("_req", "_resp") and id(n) not in de_dentro]
    for c in chamadas_req:
        metodo, caminho = c.args[0], c.args[1]
        assert isinstance(metodo, ast.Constant) and metodo.value in {"GET", "POST"}, \
            f"linha {c.lineno}: o método passado a _req/_resp tem de ser literal, GET ou POST"
        if metodo.value == "POST":
            assert isinstance(caminho, ast.Constant) and caminho.value == "/propostas", \
                f"linha {c.lineno}: POST fora de /propostas"


def test_ferramentas_so_falam_com_a_api_pelo_api_central():
    """Ferramenta que importe um cliente HTTP ou use um método que não existe no ApiCentral fura a garantia."""
    arvore = ast.parse(Path(mod_ferramentas.__file__).read_text(encoding="utf-8"))
    importados = {a.name.split(".")[0] for n in ast.walk(arvore) if isinstance(n, ast.Import) for a in n.names}
    importados |= {(n.module or "").split(".")[0] for n in ast.walk(arvore) if isinstance(n, ast.ImportFrom)}
    assert not importados & {"httpx", "httpx2", "requests", "urllib", "aiohttp", "socket"}
    usados = {n.attr for n in ast.walk(arvore)
              if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "api"}
    assert usados <= set(_metodos_publicos()), usados - set(_metodos_publicos())


@pytest.mark.parametrize("nome", sorted(NIVEIS))
async def test_cada_ferramenta_so_escreve_por_proposta(nome):
    api_falsa = ApiFalsa()
    async with api_falsa.cliente() as http:
        eventos = Eventos()
        fs = {f.name: f for f in criar_ferramentas(ApiCentral(http, "http://api.teste", TOKEN_USUARIO), eventos)}
        assert set(fs) == set(NIVEIS) == set(ENTRADAS), "ferramenta nova: classifique em NIVEIS e dê entrada aqui"
        resultado = await fs[nome].call(ENTRADAS[nome])
    if nome in LOCAIS:
        # a calculadora não fala com a API: nenhuma requisição, nem de leitura
        assert api_falsa.chamadas == [] and NIVEIS[nome] == "consulta", resultado
        return
    assert api_falsa.chamadas, f"{nome} não chamou a API: a entrada do teste não chegou até ela"
    escritas = [(c.metodo, c.caminho) for c in api_falsa.chamadas if c.metodo != "GET"]
    if NIVEIS[nome] == "consulta":
        assert escritas == []
    else:
        assert escritas == [("POST", "/propostas")]
        assert [n for n, _ in eventos.pendentes].count("proposta") == 1


def test_escritas_fora_do_api_central_sao_so_consumo_e_leitura():
    """Fora do ApiCentral (token do usuário), o agente só escreve com o token de serviço em duas rotas: o livro de
    consumo e a leitura de um comprovante (que é sugestão: o gasto nasce na confirmação de uma pessoa)."""
    from canteiro_agente import api_central

    arvore = ast.parse(inspect.getsource(api_central))
    escritoras = {}
    for f in arvore.body:
        if not isinstance(f, ast.AsyncFunctionDef):
            continue
        metodos = {n.func.attr for n in ast.walk(f) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr in {"post", "put", "patch", "delete", "request"}}
        if metodos:
            escritoras[f.name] = metodos
    assert escritoras == {"registrar_uso": {"post"}, "gravar_leitura": {"put"}}
    fonte = inspect.getsource(api_central.gravar_leitura)
    assert "/interno/comprovantes/" in fonte and "/leitura" in fonte
