"""Regras da leitura da fatura que não dependem do PDF."""
from datetime import date
from decimal import Decimal

import pytest

from conciliacao.fatura.interpretacao import (ErroCabecalho, com_ano, ler_cabecalho, ler_dia_mes, ler_valor,
                                              limpar_descricao, separar_parcela)


@pytest.mark.parametrize("texto,esperado", [
    ("R$ 1.234,56", Decimal("1234.56")),
    ("-R$ 29,17", Decimal("-29.17")),
    ("RS 5.465,76", Decimal("5465.76")),          # OCR: "$" lido como "S"
    ("RR$125,20", Decimal("125.20")),
    ("“R$29,17", Decimal("-29.17")),              # OCR: "-" lido como aspas
    ("R$ 252.432,75", Decimal("252432.75")),
    ("39.514,56", Decimal("39514.56")),
    # formato americano (fatura de maio/2026): vírgula nos milhares, ponto nos centavos
    ("R$ 269,629.73", Decimal("269629.73")),
    ("-R$ 223,442.46", Decimal("-223442.46")),
    ("R$ 29.17", Decimal("29.17")),
    ("R$ 1,598.02", Decimal("1598.02")),
])
def test_ler_valor_aceita_variacoes(texto, esperado):
    assert ler_valor(texto) == esperado


@pytest.mark.parametrize("texto", ["R$ 141111", "R$ 1,022,12", "R$1.25210", "R$ 12,5", "R$ 12.5", "R$ 1.234.56",
                                   "R$ 1,234,567", "abc", ""])
def test_ler_valor_recusa_formato_errado(texto):
    # Valor fora do formato é ilegível: nunca adivinhar.
    assert ler_valor(texto) is None


@pytest.mark.parametrize("texto,esperado", [
    ("15 JUL", (15, 7)), ("15JUL", (15, 7)), ("O1 AGO", (1, 8)), ("O02JAN", (2, 1)), ("3I DEZ", (31, 12)),
])
def test_ler_dia_mes(texto, esperado):
    assert ler_dia_mes(texto) == esperado


@pytest.mark.parametrize("texto", ["04MA", "32 JAN", "15 XYZ", ""])
def test_ler_dia_mes_recusa(texto):
    assert ler_dia_mes(texto) is None


def test_ano_vem_do_fechamento_e_parcela_antiga_e_do_ano_anterior():
    fechamento = date(2026, 9, 1)
    assert com_ano((15, 8), fechamento) == date(2026, 8, 15)
    assert com_ano((1, 9), fechamento) == date(2026, 9, 1)
    assert com_ano((14, 10), fechamento) == date(2025, 10, 14)   # "14 OUT … 11/12"
    assert com_ano((1, 12), fechamento) == date(2025, 12, 1)


def test_separar_parcela():
    assert separar_parcela("AIRBNB * HM4T4BCTZS 02/02") == ("AIRBNB * HM4T4BCTZS", 2, 2)
    assert separar_parcela("Farmacias Sao Joao s 02/02") == ("Farmacias Sao Joao s", 2, 2)
    assert separar_parcela("POSTO HAON") == ("POSTO HAON", None, None)
    assert separar_parcela("LOJA 05/03") == ("LOJA 05/03", None, None)   # 5 de 3 não é parcela


def test_limpar_descricao_do_ocr():
    assert limpar_descricao("EBN “BOOKING COM") == "EBN *BOOKING COM"
    assert limpar_descricao("INOVACAO TINTAS |") == "INOVACAO TINTAS"
    assert limpar_descricao("ILHABELA EMPORIO E €") == "ILHABELA EMPORIO E C"


TEXTO_SETEMBRO = """Esta é a fatura de setembro no valor total de R$ 257.509,11.
VENCIMENTO 11 SET 2026
REF 3 AGO A 1 SET
TOTAL VENCIMENTO
R$ 257.509,11 11 SET 2026
DATA DE FECHAMENTO DA FATURA 01/09/2026"""


def test_cabecalho_setembro():
    c = ler_cabecalho(TEXTO_SETEMBRO)
    assert c == {"periodo_ini": date(2026, 8, 3), "periodo_fim": date(2026, 9, 1), "fechamento": date(2026, 9, 1),
                 "vencimento": date(2026, 9, 11), "total": Decimal("257509.11")}


def test_cabecalho_vota_entre_leituras_do_ocr():
    # Leituras reais do OCR de agosto: "17AGO" e "N AGO" erradas, "11 AGO" certa em duas fontes.
    texto = """no valor total de RS 252.432,75.
El) VENCIMENTO 17AGO 2026
VENCIMENTO 11 AGO 2026
TOTAL R$ 252.432,75 [E] VENCIMENTO N AGO 2026
VENCIMENTO 11 AGO 2026
REFTJULATAGO
REFTJULA1AGO
REFTJULATAGO
DATA DE FECHAMENTO DA FATURA 03/08/2026"""
    c = ler_cabecalho(texto)
    assert c["vencimento"] == date(2026, 8, 11)
    assert c["fechamento"] == date(2026, 8, 3)
    assert (c["periodo_ini"], c["periodo_fim"]) == (date(2026, 7, 1), date(2026, 8, 1))
    assert c["total"] == Decimal("252432.75")


def test_cabecalho_com_totais_empatados_e_erro():
    texto = TEXTO_SETEMBRO.replace("TOTAL VENCIMENTO\nR$ 257.509,11", "TOTAL VENCIMENTO\nR$ 257.509,17")
    with pytest.raises(ErroCabecalho, match="divergentes"):
        ler_cabecalho(texto)


@pytest.mark.parametrize("trecho,esperado", [
    ("1SETA1OUT", (1, 9, 1, 10)),       # "1O" não é dia: o O é de OUT
    ("3AGOA1SET", (3, 8, 1, 9)),
    ("15JULA15AGO", (15, 7, 15, 8)),
    ("TJULATAGO", (1, 7, 1, 8)),        # OCR: "1" lido como "T"
    ("1OUTA1NOV", (1, 10, 1, 11)),
])
def test_periodo_ref_em_todos_os_meses(trecho, esperado):
    from conciliacao.fatura.interpretacao import _ref_valido
    assert _ref_valido(trecho) == [esperado]


def test_cabecalho_de_outubro():
    texto = TEXTO_SETEMBRO.replace("setembro", "outubro").replace("257.509,11", "302.050,48") \
        .replace("11 SET 2026", "11 OUT 2026").replace("REF 3 AGO A 1 SET", "REF 1 SET A 1 OUT") \
        .replace("01/09/2026", "01/10/2026")
    c = ler_cabecalho(texto)
    assert (c["periodo_ini"], c["periodo_fim"], c["vencimento"]) == (date(2026, 9, 1), date(2026, 10, 1),
                                                                    date(2026, 10, 11))
