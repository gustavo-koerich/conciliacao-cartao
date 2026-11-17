"""Importação das faturas reais (tests/fixtures, fora do git). Critérios de aceite da seção 12."""
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from conciliacao.fatura import servico
from conciliacao.models import Auditoria, Cartao, Competencia, FaturaLanc, FaturaSecao, Importacao

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).parent / "fixtures"
AGOSTO = FIXTURES / "fatura-2026-08.pdf"
SETEMBRO = FIXTURES / "fatura-2026-09.pdf"
SETEMBRO_IMPRESSA = FIXTURES / "fatura-2026-09-impressa.pdf"
MAIO = FIXTURES / "fatura-2026-05.pdf"


def _exige(arquivo):
    if not arquivo.exists():
        pytest.skip(f"fixture ausente: {arquivo.name} (fica fora do git)")


def _importar(arquivo, **kw):
    imp, criada = servico.criar_importacao(arquivo, arquivo.name, usuario="teste", ip="10.0.0.5")
    assert criada
    return servico.processar(imp.pk, **kw)


def test_setembro_pelo_texto_fecha_todos_os_portadores():
    _exige(SETEMBRO)
    imp = _importar(SETEMBRO)
    assert imp.status == "OK" and imp.ativo and imp.versao == 1
    assert imp.metodo_leitura == "TEXTO"
    assert imp.competencia.ano_mes == "2026-09"
    r = imp.resumo
    assert (r["portadores_fechados"], r["portadores"]) == (16, 16)
    assert r["total_fatura"] == r["soma_lida"] == "257509.11"
    assert Decimal(r["encargos_conta"]) == Decimal("62.92")
    assert Decimal(r["saldo_anterior"]) == Decimal("252432.75")
    assert not FaturaSecao.objects.filter(importacao=imp, status="ERRO_LEITURA").exists()
    # Compra em moeda estrangeira: valor em R$ na faixa seguinte, US$ e cotação guardados.
    garmin = FaturaLanc.objects.get(importacao=imp, descricao="Garmin", valor=Decimal("7.84"))
    assert garmin.moeda == "USD" and garmin.valor_moeda == Decimal("1.50") and garmin.cotacao == Decimal("5.2236")
    # "$ 11.293,00 U$ 285,62": moeda local, depois o valor em dólar (às vezes com "U$" em vez de "US$").
    montevideu = FaturaLanc.objects.get(importacao=imp, descricao="DNH-MTOP")
    assert (montevideu.valor, montevideu.valor_moeda) == (Decimal("1464.80"), Decimal("285.62"))
    # Descrição em 2 linhas com a parcela na linha de baixo.
    airbnb = FaturaLanc.objects.get(importacao=imp, descricao="AIRBNB * HM4T4BCTZS")
    assert (airbnb.parcela_atual, airbnb.parcela_total, airbnb.valor) == (2, 2, Decimal("3442.25"))
    # Auditoria com IP.
    reg = Auditoria.objects.get(acao="IMPORTACAO", importacao=imp)
    assert reg.resultado == "OK" and reg.ip == "10.0.0.5"


def test_cartao_desconhecido_vira_cadastro_pendente():
    _exige(SETEMBRO)
    Cartao.objects.create(final="5501", nome_fatura="FABIO LUIZ N SALES")
    imp = _importar(SETEMBRO)
    assert "5501" not in imp.resumo["cartoes_novos"]
    assert "5503" in imp.resumo["cartoes_novos"]
    assert Cartao.objects.get(final="5503").pendente_cadastro
    assert not Cartao.objects.get(final="5501").pendente_cadastro


def test_reimportar_o_mesmo_arquivo_nao_cria_nada():
    _exige(SETEMBRO)
    _importar(SETEMBRO)
    antes = (Importacao.objects.count(), FaturaLanc.objects.count())
    imp, criada = servico.criar_importacao(SETEMBRO, SETEMBRO.name, usuario="teste", ip="10.0.0.5")
    assert not criada and imp.ativo
    assert (Importacao.objects.count(), FaturaLanc.objects.count()) == antes


def test_segunda_versao_fica_aguardando_confirmacao(tmp_path):
    _exige(SETEMBRO)
    _importar(SETEMBRO)
    copia = tmp_path / "setembro-de-novo.pdf"
    shutil.copy(SETEMBRO, copia)
    with open(copia, "ab") as f:
        f.write(b"\n%% copia com outro hash\n")
    nova = _importar(copia)
    assert nova.status == "OK" and not nova.ativo and nova.versao == 2
    assert nova.resumo["aguardando_confirmacao"] and nova.resumo["versao_ativa"] == 1
    assert Importacao.objects.filter(tipo="FATURA", ativo=True).count() == 1


def test_competencia_fechada_recusa_fatura():
    _exige(SETEMBRO)
    _importar(SETEMBRO)
    Competencia.objects.filter(ano_mes="2026-09").update(status="FECHADA")
    copia = FIXTURES.parent / "_tmp_setembro.pdf"
    try:
        shutil.copy(SETEMBRO, copia)
        with open(copia, "ab") as f:
            f.write(b"\n%% outra\n")
        imp = _importar(copia)
    finally:
        copia.unlink(missing_ok=True)
    assert imp.status == "ERRO" and "FECHADA" in imp.resumo["erro"]
    assert not FaturaLanc.objects.filter(importacao=imp).exists()


def test_impressao_da_pagina_web_e_recusada():
    _exige(SETEMBRO_IMPRESSA)
    imp = _importar(SETEMBRO_IMPRESSA)
    assert imp.status == "ERRO" and not imp.ativo
    assert "tabela de lançamentos" in imp.resumo["erro"]
    assert Auditoria.objects.get(importacao=imp).resultado == "ERRO"


def test_arquivo_que_nao_e_pdf_e_recusado(tmp_path):
    falso = tmp_path / "fatura.pdf"
    falso.write_bytes(b"isto nao e um pdf")
    with pytest.raises(servico.ArquivoRecusado, match="não é um PDF"):
        servico.criar_importacao(falso, falso.name)
    with pytest.raises(servico.ArquivoRecusado, match="PDF"):
        servico.criar_importacao(falso, "fatura.xlsx")


def test_agosto_em_imagem_pelo_ocr_atende_o_criterio_de_aceite():
    """Seção 12: os cartões fecham ao centavo sem IA; total 252.432,75; encargos (sem saldo/pagamento) 201,91."""
    _exige(AGOSTO)
    imp = _importar(AGOSTO)
    r = imp.resumo
    assert imp.metodo_leitura == "OCR"
    assert imp.status == "OK" and imp.ativo, r.get("erros")
    assert imp.competencia.ano_mes == "2026-08"
    assert r["portadores_fechados"] == r["portadores"] == 17
    assert r["total_fatura"] == r["soma_lida"] == "252432.75"
    assert Decimal(r["encargos_conta"]) == Decimal("201.91")
    comp = imp.competencia
    assert (str(comp.periodo_ini), str(comp.fechamento), str(comp.vencimento)) == ("2026-07-01", "2026-08-03",
                                                                                   "2026-08-11")
    # Fábio (5501): os dois lançamentos do Tupinambá (+150,75 / -150,75) foram lidos com o sinal certo.
    fabio = FaturaLanc.objects.filter(importacao=imp, cartao__final="5501")
    assert fabio.filter(valor=Decimal("150.75")).exists() and fabio.filter(valor=Decimal("-150.75")).exists()


def test_maio_com_valores_em_formato_americano_fecha():
    """Maio/2026: R$ em "269,629.73" (cotações seguem "4,9653"); pág. 4 continua a tabela sem cabeçalho;
    a conta começa no pé da coluna esquerda da pág. 3, com quadros de resumo no alto da coluna direita."""
    _exige(MAIO)
    imp = _importar(MAIO)
    assert imp.status == "OK" and imp.ativo and imp.competencia.ano_mes == "2026-05"
    r = imp.resumo
    assert (r["portadores_fechados"], r["portadores"]) == (16, 16) and r["lancamentos"] == 729
    assert r["total_fatura"] == r["soma_lida"] == "269629.73"
    assert not FaturaSecao.objects.filter(importacao=imp, status="ERRO_LEITURA").exists()
    # primeira linha da pág. 4 (acima da margem fixa do cabeçalho)
    assert FaturaLanc.objects.filter(importacao=imp, descricao="IOF OPERACAO EXTERIOR", valor=Decimal("2.62")).exists()
    hertz = FaturaLanc.objects.get(importacao=imp, descricao="HERTZ")
    assert (hertz.valor, hertz.moeda, hertz.valor_moeda, hertz.cotacao) == (
        Decimal("1011.28"), "USD", Decimal("201.92"), Decimal("5.0083"))
