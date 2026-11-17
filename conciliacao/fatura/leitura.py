"""Extração das palavras da fatura: pela camada de texto do PDF ou por OCR local (Tesseract).

Os dois caminhos entregam a mesma coisa (Palavra com posição em pontos) e passam pela mesma montagem de segmentos
(layout.py) e pela mesma interpretação (interpretacao.py). Só muda de onde vem o texto.

OCR: a página é renderizada a 300 dpi e cada coluna é lida em tiras verticais (data | descrição | cidade+valor),
para que as linhas deslocadas de uma descrição em 2 linhas não se misturem com a linha da data e do valor.
Os trechos entre faixas (nome do portador, "TOTAL DE X") são lidos à parte, na largura inteira da coluna.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np
import pdfplumber
import pytesseract
from PIL import Image

from . import layout
from .interpretacao import MESES
from .layout import Palavra

log = logging.getLogger(__name__)

DPI = 300
ESCALA = DPI / 72
IDIOMA = "por"
CONFIG_BLOCO = "--oem 1 --psm 6"
# Coluna de data: só dígitos e as letras dos meses (JAN FEV MAR ABR MAI JUN JUL AGO SET OUT NOV DEZ).
CONFIG_DATA = "--oem 1 --psm 6 -c tessedit_char_whitelist=0123456789ABDEFGIJLMNOPRSTUVZ"
CONFIG_PAGINA = "--oem 1 --psm 3"
PAGINAS_CABECALHO = 3          # resumo da fatura: páginas 1 a 3
MIN_CARACTERES_TEXTO = 200     # abaixo disso a página de lançamentos não tem texto utilizável
TRABALHADORES_OCR = 4


class ErroLeitura(Exception):
    pass


@dataclass
class LeituraBruta:
    metodo: str                                    # "TEXTO" ou "OCR"
    texto_cabecalho: str
    segmentos: list = field(default_factory=list)
    paginas_lancamentos: list = field(default_factory=list)


def _paginas_com_faixas(pdf):
    paginas = [(i, layout.faixas_da_pagina(p.rects)) for i, p in enumerate(pdf.pages)]
    return [(i, faixas) for i, faixas in paginas if faixas]


def ler_pdf(caminho, progresso=None, forcar_ocr=False) -> LeituraBruta:
    """Lê a fatura. Usa a camada de texto quando existe; senão, OCR. progresso(feito, total, etapa) é opcional."""
    with pdfplumber.open(caminho) as pdf:
        paginas = _paginas_com_faixas(pdf)
        if not paginas:
            raise ErroLeitura("Nenhuma página com a tabela de lançamentos foi reconhecida (layout Ailos).")
        caracteres = sum(len(pdf.pages[i].chars) for i, _ in paginas)
        if caracteres >= MIN_CARACTERES_TEXTO * len(paginas) and not forcar_ocr:
            return _ler_texto(pdf, paginas, progresso)
        return _ler_ocr(pdf, paginas, progresso)


# ---------- camada de texto ----------

def _palavras_texto(pagina):
    return [Palavra(w["text"], w["x0"], w["x1"], w["top"], w["bottom"])
            for w in pagina.extract_words(y_tolerance=1, keep_blank_chars=False)]


def _ler_texto(pdf, paginas, progresso):
    cabecalho = "\n".join(pdf.pages[i].extract_text() or "" for i in range(min(PAGINAS_CABECALHO, len(pdf.pages))))
    segmentos = []
    for feito, (i, faixas) in enumerate(paginas, start=1):
        segmentos += layout.montar_segmentos(i + 1, faixas, _palavras_texto(pdf.pages[i]))
        if progresso:
            progresso(feito, len(paginas), "lendo texto")
    return LeituraBruta("TEXTO", cabecalho, segmentos, [i + 1 for i, _ in paginas])


# ---------- OCR ----------

DPI_CABECALHO = 400
REF_RECORTE = (50, 25, 300, 60)        # "EMPRESA DEMO LTDA / REF 1 JUL A 1 AGO" no topo de cada página
ROTULOS_AMPLIADOS = ("VENCIMENTO", "FECHAMENTO")
# Segunda leitura de uma célula que não fez sentido: recorte ampliado, uma linha só, caracteres restritos.
AMPLIACAO_RELEITURA = 2
CONFIG_RELER_DATA = "--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789ABDEFGIJLMNOPRSTUVZ"
CONFIG_RELER_VALOR = "--oem 1 --psm 7 -c tessedit_char_whitelist=R$-0123456789.,"
RELER_VALOR_INI = 150                  # a partir de col0+150: pega o valor inteiro mesmo quando é longo
CONFIG_RELER_LINHA = "--oem 1 --psm 7"
DESC_DUAS_LINHAS_CORTE = 9.5           # pt a partir do topo da faixa: separa as 2 linhas da descrição


def _ocr_palavras(imagem, origem_x_pt, origem_y_pt, config=CONFIG_BLOCO):
    """OCR de um recorte; devolve palavras com posição em pontos da página."""
    dados = pytesseract.image_to_data(imagem, lang=IDIOMA, config=config, output_type=pytesseract.Output.DICT)
    palavras = []
    for texto, conf, x, y, w, h in zip(dados["text"], dados["conf"], dados["left"], dados["top"],
                                       dados["width"], dados["height"]):
        texto = (texto or "").strip()
        if not texto or float(conf) < 0:
            continue
        palavras.append(Palavra(texto, origem_x_pt + x / ESCALA, origem_x_pt + (x + w) / ESCALA,
                                origem_y_pt + y / ESCALA, origem_y_pt + (y + h) / ESCALA))
    return palavras


def _recorte(imagem, x0, top, x1, bottom):
    return imagem.crop((round(x0 * ESCALA), round(top * ESCALA), round(x1 * ESCALA), round(bottom * ESCALA)))


def _reler(imagem, x0, top, x1, bottom, config):
    recorte = _recorte(imagem, x0, top, x1, bottom)
    ampliado = recorte.resize((recorte.width * AMPLIACAO_RELEITURA, recorte.height * AMPLIACAO_RELEITURA),
                              Image.LANCZOS)
    return pytesseract.image_to_string(ampliado, lang=IDIOMA, config=config).strip()


def _dentro_de_faixa(p, faixas):
    return any(f.top <= p.centro_y < f.bottom for f in faixas)


def _vermelho(imagem_rgb, x0, top, x1, bottom):
    """Valores negativos são impressos em vermelho na fatura."""
    px = np.asarray(_recorte(imagem_rgb, x0, top, x1, bottom), dtype=np.int16)
    vermelhos = (px[..., 0] > 150) & (px[..., 1] < 110) & (px[..., 2] < 110)
    return int(vermelhos.sum()) >= 15


def _palavras_ocr(imagem_rgb, imagem, faixas_por_coluna):
    palavras = []
    for col0, faixas in faixas_por_coluna.items():
        topo, base = layout.AREA_TOPO, layout.AREA_BASE
        # Tiras verticais: só as palavras que caem dentro de faixas (linhas da tabela) são aproveitadas.
        tiras = [
            (col0, col0 + layout.DATA_FIM, lambda r: True, CONFIG_DATA),
            (col0 + layout.DATA_FIM, col0 + 180, lambda r: r < layout.CIDADE_INI, CONFIG_BLOCO),
            (col0 + layout.CIDADE_INI - 4, col0 + layout.COLUNA_LARGURA, lambda r: r >= layout.CIDADE_INI - 1,
             CONFIG_BLOCO),
        ]
        for x0, x1, aceita, config in tiras:
            for p in _ocr_palavras(_recorte(imagem, x0, topo, x1, base), x0, topo, config):
                if _dentro_de_faixa(p, faixas) and aceita(p.x0 - col0):
                    palavras.append(p)
        inicio_valor = col0 + layout.VALOR_INI - 20
        for f in faixas:
            if _vermelho(imagem_rgb, inicio_valor, f.top, col0 + layout.COLUNA_LARGURA, f.bottom):
                for p in palavras:
                    if f.top <= p.centro_y < f.bottom and inicio_valor <= p.x0 < col0 + layout.COLUNA_LARGURA:
                        p.vermelho = True
        # Trechos livres entre faixas (nome do portador, totais): largura inteira da coluna.
        limites = [topo] + [y for f in faixas for y in (f.top, f.bottom)] + [base]
        for cima, baixo in zip(limites[::2], limites[1::2]):
            if baixo - cima < 6:
                continue
            recorte = _recorte(imagem, col0 - 2, cima, col0 + layout.COLUNA_LARGURA, baixo)
            for p in _ocr_palavras(recorte, col0 - 2, cima):
                if not _dentro_de_faixa(p, faixas):
                    palavras.append(p)
    return palavras


def _datas_segunda_leitura(imagem, faixas_por_coluna):
    """Lê a coluna de datas de novo, ampliada 1,5x: {(col0, topo da faixa): texto}. Serve para conferir a primeira."""
    resultado = {}
    for col0, faixas in faixas_por_coluna.items():
        recorte = _recorte(imagem, col0, layout.AREA_TOPO, col0 + layout.DATA_FIM, layout.AREA_BASE)
        ampliado = recorte.resize((int(recorte.width * 1.5), int(recorte.height * 1.5)), Image.LANCZOS)
        dados = pytesseract.image_to_data(ampliado, lang=IDIOMA, config=CONFIG_DATA,
                                          output_type=pytesseract.Output.DICT)
        for texto, y, h in zip(dados["text"], dados["top"], dados["height"]):
            texto = (texto or "").strip()
            if not texto:
                continue
            centro = layout.AREA_TOPO + (y + h / 2) / 1.5 / ESCALA
            for f in faixas:
                if f.top <= centro < f.bottom:
                    chave = (col0, round(f.top, 1))
                    resultado[chave] = (resultado.get(chave, "") + " " + texto).strip()
    return resultado


def _desc_em_duas_linhas(imagem, seg):
    """Descrição de uma linha fica centrada (de +5,5 a +12,5 pt); a de duas linhas ocupa de +2 a +17 pt.
    Tinta perto do topo E perto do pé da célula = duas linhas."""
    x0, x1 = seg.col0 + layout.DATA_FIM, seg.col0 + layout.CIDADE_INI

    def tem_tinta(cima, baixo):
        px = np.asarray(_recorte(imagem, x0, seg.top + cima, x1, seg.top + baixo))
        return int((px < 200).sum()) >= 30  # texto é cinza (~130); fundo 239 (faixa cinza) ou 255

    return tem_tinta(1.5, 4.5) and tem_tinta(14, 17)


def _ocr_pagina(numero, imagem_rgb, faixas_por_coluna):
    """OCR de uma página com conferência:
    - data: duas leituras independentes; se divergirem, uma terceira (célula ampliada) desempata. Sem maioria, a
      data fica ilegível e a seção não fecha (melhor parar do que gravar data errada);
    - valor que não está no formato "1.234,56": relido na célula ampliada (a trava de totais confere o resto);
    - descrição em 2 linhas: cada linha relida separadamente (as duas ficam a +2 e +10 pt do topo da faixa)."""
    from .interpretacao import ler_dia_mes, ler_valor

    imagem = imagem_rgb.convert("L")
    palavras = _palavras_ocr(imagem_rgb, imagem, faixas_por_coluna)
    segmentos = layout.montar_segmentos(numero, faixas_por_coluna, palavras)
    segunda = _datas_segunda_leitura(imagem, faixas_por_coluna)
    estat = {"datas_divergentes": 0, "datas_sem_maioria": 0, "valores_relidos": 0, "descricoes_relidas": 0}
    for seg in segmentos:
        if seg.tipo != "faixa":
            continue
        if seg.data:
            leituras = [ler_dia_mes(seg.data), ler_dia_mes(segunda.get((seg.col0, round(seg.top, 1)), ""))]
            if leituras[0] is None or leituras[0] != leituras[1]:
                estat["datas_divergentes"] += 1
                terceira = _reler(imagem, seg.col0, seg.top, seg.col0 + layout.DATA_FIM, seg.bottom,
                                  CONFIG_RELER_DATA)
                leituras.append(ler_dia_mes(terceira))
                validas = [l for l in leituras if l]
                maioria = next((l for l in validas if validas.count(l) >= 2), None)
                if maioria:
                    seg.data = f"{maioria[0]:02d}{next(k for k, v in MESES.items() if v == maioria[1])}"
                else:
                    estat["datas_sem_maioria"] += 1
                    seg.data = "ILEGIVEL"  # sem maioria: a data fica ilegível de propósito
        if seg.valor and ler_valor(seg.valor) is None:
            estat["valores_relidos"] += 1
            novo = _reler(imagem, seg.col0 + RELER_VALOR_INI, seg.top, seg.col0 + layout.COLUNA_LARGURA,
                          seg.bottom, CONFIG_RELER_VALOR)
            novo = novo.split()[-1] if novo.split() else ""
            if ler_valor(novo) is not None:
                negativo = seg.valor.startswith("-") and not novo.startswith("-")
                seg.valor = ("-" if negativo else "") + novo
        if _desc_em_duas_linhas(imagem, seg):
            estat["descricoes_relidas"] += 1
            meio = seg.top + DESC_DUAS_LINHAS_CORTE
            x0, x1 = seg.col0 + layout.DATA_FIM, seg.col0 + layout.CIDADE_INI
            linhas = [_reler(imagem, x0, seg.top, x1, meio, CONFIG_RELER_LINHA),
                      _reler(imagem, x0, meio, x1, seg.bottom, CONFIG_RELER_LINHA)]
            linhas = [" ".join(l.split()) for l in linhas if l.strip()]
            if linhas:
                seg.descricao = linhas
    if any(estat.values()):
        log.info("OCR: conferência da página", extra={"pagina": numero, **estat})
    return segmentos


def _ocr_cabecalho(pdf, paginas):
    """Texto do resumo para a votação em interpretacao.ler_cabecalho: páginas 1 a 3 inteiras, o REF do topo de
    algumas páginas de lançamentos e, ampliado, o trecho logo à direita de cada "VENCIMENTO"/"FECHAMENTO"."""
    escala = DPI_CABECALHO / 72
    partes = []
    for i in range(min(PAGINAS_CABECALHO, len(pdf.pages))):
        imagem = pdf.pages[i].to_image(resolution=DPI_CABECALHO).original.convert("L")
        partes.append(pytesseract.image_to_string(imagem, lang=IDIOMA, config=CONFIG_PAGINA))
        dados = pytesseract.image_to_data(imagem, lang=IDIOMA, config=CONFIG_PAGINA,
                                          output_type=pytesseract.Output.DICT)
        for texto, x, y, w, h in zip(dados["text"], dados["left"], dados["top"], dados["width"], dados["height"]):
            rotulo = (texto or "").strip()
            if rotulo not in ROTULOS_AMPLIADOS:
                continue
            recorte = imagem.crop((x + w, y - int(h * 0.6), x + w + int(160 * escala), y + int(h * 1.6)))
            trecho = pytesseract.image_to_string(recorte, lang=IDIOMA, config="--oem 1 --psm 7").strip()
            partes.append(f"{rotulo} {trecho}")
    for i, _ in paginas[:4]:
        imagem = pdf.pages[i].to_image(resolution=DPI_CABECALHO).original.convert("L")
        recorte = imagem.crop(tuple(round(v * escala) for v in REF_RECORTE))
        partes.append(pytesseract.image_to_string(recorte, lang=IDIOMA, config=CONFIG_BLOCO))
    return "\n".join(partes)


def _ler_ocr(pdf, paginas, progresso):
    total = len(paginas) + 1
    cabecalho = _ocr_cabecalho(pdf, paginas)
    if progresso:
        progresso(1, total, "OCR do resumo")
    segmentos = []
    feito = 1
    # O pdfium (renderização) não aceita threads: renderiza aqui, uma página por vez, e só o Tesseract
    # (processo externo) roda em paralelo. Lotes pequenos limitam a memória das imagens a 300 dpi.
    with ThreadPoolExecutor(max_workers=TRABALHADORES_OCR) as executor:
        for inicio in range(0, len(paginas), TRABALHADORES_OCR):
            lote = paginas[inicio:inicio + TRABALHADORES_OCR]
            futuros = [executor.submit(_ocr_pagina, i + 1,
                                       pdf.pages[i].to_image(resolution=DPI).original.convert("RGB"), faixas)
                       for i, faixas in lote]
            for futuro in futuros:
                segmentos += futuro.result()
                feito += 1
                if progresso:
                    progresso(feito, total, "OCR dos lançamentos")
    return LeituraBruta("OCR", cabecalho, segmentos, [i + 1 for i, _ in paginas])
