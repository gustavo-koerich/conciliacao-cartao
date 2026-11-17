"""Fatura do Banco do Brasil (PDF e Excel) e convivência com a Ailos (vários bancos)."""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from conciliacao.fatura import servico
from conciliacao.models import Cartao, Competencia, ContaCartao, FaturaLanc

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).parent / "fixtures"
PDF_SET = FIXTURES / "fatura-bb-2026-09.pdf"
XLSX_OUT = FIXTURES / "fatura-bb-2026-10.xlsx"


@pytest.fixture
def bb():
    return ContaCartao.objects.create(nome="Banco do Brasil Ourocard Empresa", banco="BB", slug="bb", leitor="BB")


def _importar(arquivo, conta):
    if not arquivo.exists():
        pytest.skip(f"fixture ausente: {arquivo.name}")
    imp, criada = servico.criar_importacao(arquivo, arquivo.name, conta=conta, usuario="teste", ip="10.0.0.5")
    assert criada
    return servico.processar(imp.pk)


def test_bb_setembro_em_pdf(bb):
    imp = _importar(PDF_SET, bb)
    r = imp.resumo
    assert imp.status == "OK" and imp.ativo, r.get("erros")
    assert imp.competencia.conta == bb and imp.competencia.codigo == "bb-2026-09"
    assert (r["portadores_fechados"], r["portadores"]) == (1, 1)
    assert r["total_fatura"] == r["soma_lida"] == "52646.23"
    assert str(imp.competencia.fechamento) == "2026-09-04" and str(imp.competencia.vencimento) == "2026-09-17"
    assert not r["fechamento_estimado"]
    cartao = Cartao.objects.get(conta=bb, final="5506")
    assert cartao.pendente_cadastro
    euro = FaturaLanc.objects.filter(importacao=imp, descricao__startswith="THERENVA").order_by("data").first()
    assert (euro.valor, euro.moeda, euro.valor_moeda, euro.cotacao) == (Decimal("1836.04"), "EUR", Decimal("345.12"),
                                                                       Decimal("5.32"))
    parcela = FaturaLanc.objects.get(importacao=imp, descricao="FRIGELAR FI SAO JOSE")
    assert (parcela.data, parcela.parcela_atual, parcela.parcela_total) == (date(2026, 1, 5), 8, 8)
    pagamento = FaturaLanc.objects.get(importacao=imp, natureza="PAGAMENTO")
    assert pagamento.valor == Decimal("-60734.71") and pagamento.secao == "CONTA"


def test_bb_outubro_em_excel_usa_fechamento_anterior(bb):
    _importar(PDF_SET, bb)
    imp = _importar(XLSX_OUT, bb)
    r = imp.resumo
    assert imp.status == "OK" and imp.ativo, r.get("erros")
    assert imp.competencia.codigo == "bb-2026-10" and r["fechamento_estimado"]
    assert r["total_fatura"] == r["soma_lida"] == "79649.12"
    assert str(imp.competencia.periodo_ini) == "2026-09-05"     # dia seguinte ao fechamento de setembro
    assert Cartao.objects.filter(conta=bb, final="5505").exists()
    therenva = FaturaLanc.objects.filter(importacao=imp, descricao__startswith="THERENVA").first()
    assert therenva.moeda == "EUR" and therenva.cotacao is None  # o Excel do BB corta a cotação


def test_formato_errado_para_o_banco_e_recusado(tmp_path):
    ailos = ContaCartao.objects.get(slug="ailos") if ContaCartao.objects.filter(slug="ailos").exists() else None
    if not XLSX_OUT.exists():
        pytest.skip("fixture ausente")
    with pytest.raises(servico.ArquivoRecusado, match="PDF"):
        servico.criar_importacao(XLSX_OUT, XLSX_OUT.name, conta=ailos)   # Ailos só aceita PDF


def test_mesmo_mes_em_bancos_diferentes_sao_competencias_separadas(bb):
    _importar(PDF_SET, bb)
    ailos = Competencia.objects.create(ano_mes="2026-09", periodo_ini=date(2026, 8, 3), periodo_fim=date(2026, 9, 1),
                                       fechamento=date(2026, 9, 1), vencimento=date(2026, 9, 11),
                                       total_fatura=Decimal("1"))
    assert Competencia.objects.filter(ano_mes="2026-09").count() == 2
    assert {c.codigo for c in Competencia.objects.all()} == {"ailos-2026-09", "bb-2026-09"} and ailos.conta.slug == "ailos"


def test_tela_de_fatura_pede_o_banco(client, bb):
    pagina = client.get("/fatura/", REMOTE_ADDR="10.0.0.5").content.decode()
    assert "Banco do Brasil Ourocard Empresa (PDF, XLSX)" in pagina
    resp = client.post("/fatura/", {}, REMOTE_ADDR="10.0.0.5", follow=True)
    assert "Escolha o banco e o arquivo" in resp.content.decode()
