"""Geometria da fatura Ailos (relatório JasperReports, A4 em pontos).

Cada página de lançamentos tem 2 colunas. Cada linha da tabela é uma faixa de ~18 pt desenhada como retângulo
(fundo cinza e branco alternados). Isso vale tanto para o PDF com texto quanto para o PDF "impresso" em que o texto
virou curvas: nos dois a faixa existe como retângulo vetorial. As células ficam em posições fixas a partir da borda
esquerda da coluna (col0).

Fora das faixas ficam: cabeçalho da tabela, nome do portador + final, "TOTAL DE X" e o "TOTAL" geral.
"""
import re
from dataclasses import dataclass, field

# Faixa = retângulo de linha da tabela
FAIXA_LARGURA_MIN, FAIXA_LARGURA_MAX = 250, 270
FAIXA_ALTURA_MIN, FAIXA_ALTURA_MAX = 16, 20
COLUNA_LARGURA = 262

# Células, relativas a col0 (medidas na fatura de setembro/2026)
DATA_FIM = 41           # data: col0 .. col0+41
DESC_INI = 41           # descrição começa em col0+42
CIDADE_INI = 132        # palavra que começa a partir daqui é cidade (ou valor)
VALOR_INI = 195         # "R$"/"-R$" e o número começam a partir daqui

# Área útil da página (abaixo do cabeçalho "EMPRESA DEMO LTDA / REF …" e acima do rodapé "Página x de y")
AREA_TOPO, AREA_BASE = 80, 802
TOLERANCIA_LINHA = 2.5  # pt: palavras com topo até essa distância formam a mesma linha


@dataclass
class Palavra:
    texto: str
    x0: float
    x1: float
    top: float
    bottom: float
    vermelho: bool = False      # OCR: valor impresso em vermelho = negativo (o "-" às vezes some ou vira aspas)

    @property
    def centro_y(self):
        return (self.top + self.bottom) / 2


@dataclass
class Faixa:
    top: float
    bottom: float


@dataclass
class Segmento:
    """Uma faixa (linha da tabela) ou um trecho livre entre faixas, já separado por célula."""
    pagina: int
    coluna: int                 # 0 = esquerda, 1 = direita
    top: float
    tipo: str                   # "faixa" | "livre"
    col0: float = 0
    bottom: float = 0
    data: str = ""
    descricao: list = field(default_factory=list)   # linhas da descrição (1 ou 2)
    cidade: str = ""
    valor: str = ""
    linhas: list = field(default_factory=list)      # só para "livre": linhas de texto completas

    def resumo(self):
        if self.tipo == "livre":
            return " | ".join(self.linhas)
        return f"{self.data} | {' / '.join(self.descricao)} | {self.cidade} | {self.valor}"


def faixas_da_pagina(retangulos) -> dict:
    """Agrupa os retângulos de linha por coluna: {col0: [Faixa…]} ordenadas de cima para baixo."""
    colunas = {}
    for r in retangulos:
        largura, altura = r["x1"] - r["x0"], r["bottom"] - r["top"]
        if FAIXA_LARGURA_MIN < largura < FAIXA_LARGURA_MAX and FAIXA_ALTURA_MIN < altura < FAIXA_ALTURA_MAX:
            colunas.setdefault(round(r["x0"]), []).append(Faixa(r["top"], r["bottom"]))
    resultado = {}
    for col0, faixas in colunas.items():
        unicas = {}
        for f in sorted(faixas, key=lambda f: f.top):
            unicas.setdefault(round(f.top, 1), f)
        resultado[col0] = list(unicas.values())
    return dict(sorted(resultado.items()))


def _linhas(palavras):
    """Agrupa palavras em linhas visuais (topo próximo), da esquerda para a direita."""
    linhas = []
    for p in sorted(palavras, key=lambda p: (p.top, p.x0)):
        if linhas and abs(linhas[-1][0].top - p.top) <= TOLERANCIA_LINHA:
            linhas[-1].append(p)
        else:
            linhas.append([p])
    return [sorted(linha, key=lambda p: p.x0) for linha in linhas]


# Lixo de OCR antes do sinal: aspas, til e travessões costumam ser o "-" mal lido.
_SUJEIRA = "\"'“”‘’~–—_"
_MOEDA = re.compile(r"^[" + re.escape(_SUJEIRA) + r"-]?R{1,2}[\$S5]?\$?$", re.I)
_NUMERO = re.compile(r"^[" + re.escape(_SUJEIRA) + r"-]?(R{1,2}[\$S5]{1,2})?-?[\d.,]*\d$", re.I)


def _valor_no_fim(direita, col0):
    """O valor é o último grupo da linha: "R$ 1.234,56", "-R$ 12,00", "RS 12,00" (OCR) ou "R$1.234,56" colado.
    Só vale se começar na região da coluna de valor."""
    if not direita or not _NUMERO.match(direita[-1].texto) or direita[-1].x0 - col0 < VALOR_INI - 20:
        return []
    grupo = [direita[-1]]
    if len(direita) > 1 and _MOEDA.match(direita[-2].texto) and direita[-2].x0 - col0 >= VALOR_INI - 20:
        grupo.insert(0, direita[-2])
    return grupo


def _texto(palavras):
    return " ".join(p.texto for p in palavras).strip()


def montar_segmentos(pagina: int, colunas_faixas: dict, palavras: list) -> list:
    """Distribui as palavras da página nas faixas e trechos livres de cada coluna, na ordem de leitura."""
    segmentos = []
    for indice, (col0, faixas) in enumerate(colunas_faixas.items()):
        # Página que continua a tabela sem o cabeçalho "DATA / DESCRIÇÃO" começa mais alto (ex.: maio/2026,
        # pág. 4, faixa em 66 pt): a área útil começa na primeira faixa, se ela estiver acima de AREA_TOPO.
        topo = min(AREA_TOPO, faixas[0].top) if faixas else AREA_TOPO
        da_coluna = [p for p in palavras
                     if col0 - 2 <= p.x0 < col0 + COLUNA_LARGURA and topo <= p.centro_y <= AREA_BASE]
        usadas = set()
        for faixa in faixas:
            dentro = [p for p in da_coluna if faixa.top <= p.centro_y < faixa.bottom]
            usadas.update(id(p) for p in dentro)
            if not dentro:
                continue
            seg = Segmento(pagina, indice, faixa.top, "faixa", col0=col0, bottom=faixa.bottom)
            rel = lambda p: p.x0 - col0  # noqa: E731
            seg.data = _texto(sorted((p for p in dentro if rel(p) < DATA_FIM), key=lambda p: p.x0))
            direita = sorted((p for p in dentro if rel(p) >= CIDADE_INI), key=lambda p: p.x0)
            valor = _valor_no_fim(direita, col0)
            seg.valor = _texto(valor)
            if any(p.vermelho for p in valor) and not seg.valor.startswith("-"):
                seg.valor = "-" + seg.valor.lstrip("\"'“”‘’~–—")
            seg.cidade = _texto(p for p in direita if p not in valor)
            seg.descricao = [_texto(linha) for linha in _linhas(
                [p for p in dentro if DATA_FIM <= rel(p) < CIDADE_INI])]
            segmentos.append(seg)
        livres = [p for p in da_coluna if id(p) not in usadas]
        for linha in _linhas(livres):
            segmentos.append(Segmento(pagina, indice, linha[0].top, "livre", linhas=[_texto(linha)]))
    # ordem de leitura: coluna esquerda inteira, depois a direita; de cima para baixo
    return sorted(segmentos, key=lambda s: (s.coluna, s.top))
