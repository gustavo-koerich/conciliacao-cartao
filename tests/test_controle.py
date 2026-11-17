"""Planilha CONTROLE DE COMPRAS CARTÃO: importação, correspondência com a fatura e efeito no motor e nas notas."""
import io
from datetime import datetime

import openpyxl
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from conciliacao import controle, motor
from conciliacao.models import ControleCompra, Importacao, Resultado
from tests.test_notas import _cenario, _enviar, nfe

pytestmark = pytest.mark.django_db

CABECALHO = ["NF", "Data da compra", "Produto", "QUANTIDADE", "PREÇO UNITÁRIO", "TOTAL DA COMPRA ", "TOTAL NA FATURA",
             "PARCELA", "PAGAMENTO", "SETOR", "OC", "OBSERVAÇÃO"]


def planilha(*linhas, aba="COMPRAS"):
    livro = openpyxl.Workbook()
    livro.active.title = "RESUMO"
    folha = livro.create_sheet(aba)
    folha.append(CABECALHO)
    for l in linhas:
        folha.append(l)
    buf = io.BytesIO()
    livro.save(buf)
    return SimpleUploadedFile("CONTROLE DE COMPRAS CARTÃO 2026.xlsx", buf.getvalue())


def _linha(nf, dia, produto, total, fatura, parcela=1, pagamento="ELO 4135", setor="COMPRAS", oc=True):
    return [nf, datetime(2026, 7, dia), produto, 1, total, total, fatura, parcela, pagamento, setor, oc, None]


def test_importa_por_cabecalho_ignora_pagamento_e_versiona():
    arq = planilha(_linha("NF 000123", 14, "Licença WIX", 384, 384),
                   _linha("SEM NF", 15, "Café", 50, 50),
                   _linha("CANCELADA", 16, "Cadeira", 200, -200),
                   [None, None, None, None, None, None, None, None, None, None, None, None])
    imp, nova = controle.importar(arq, arq.name, usuario="t")
    assert nova and imp.ativo and imp.resumo == {"linhas": 3, "ignoradas": 1, "com_nf": 1, "canceladas": 1}
    l = ControleCompra.objects.get(produto="Licença WIX")
    assert (l.nf_numero, l.parcelas, l.oc, l.setor) == ("000123", 1, True, "COMPRAS")
    assert ControleCompra.objects.get(produto="Café").nf_numero == ""
    arq.seek(0)
    assert controle.importar(arq, arq.name)[1] is False
    nova_versao = planilha(_linha("NF 9", 14, "Outra", 10, 10))
    imp2, _ = controle.importar(nova_versao, nova_versao.name)
    assert imp2.versao == imp.versao + 1
    assert list(Importacao.objects.filter(tipo="CONTROLE", ativo=True)) == [imp2]


def test_planilha_sem_as_colunas_e_recusada():
    livro = openpyxl.Workbook()
    livro.active.append(["A", "B"])
    buf = io.BytesIO()
    livro.save(buf)
    with pytest.raises(controle.ControleInvalido):
        controle.importar(SimpleUploadedFile("x.xlsx", buf.getvalue()), "x.xlsx")


def test_compra_sem_nf_no_controle_fica_conciliada_com_ressalva():
    c, vista, parcela = _cenario()
    controle.importar(planilha(_linha("", 13, "Licença WIX anual", 384, 384, setor="TI")), "c.xlsx")
    motor.executar(c.comp)
    r = Resultado.objects.get(vigente=True, fatura_lanc=vista)
    assert r.status == "CONCILIADO_RESSALVA"
    assert "sem nota fiscal" in r.motivo and "Licença WIX anual" in r.motivo and "setor TI" in r.motivo
    assert Resultado.objects.get(vigente=True, fatura_lanc=parcela).status == "SEM_NOTA"


def test_outro_valor_nao_corresponde():
    c, _, _ = _cenario()
    controle.importar(planilha(_linha("NF 1", 14, "Outro valor", 384.01, 384.01)), "c.xlsx")
    motor.executar(c.comp)
    assert set(Resultado.objects.filter(vigente=True).values_list("status", flat=True)) == {"SEM_NOTA"}


def test_mesmo_valor_com_data_ou_parcelas_diferentes_fica_com_ressalva():
    c, vista, parcela = _cenario()
    controle.importar(planilha(_linha("NF 1", 20, "Longe", 384, 384),             # 6 dias
                               _linha("NF 2", 1, "Parcelada", 11509.20, 1918.20)),  # controle diz à vista; fatura 2/6
                      "c.xlsx")
    motor.executar(c.comp)
    r1 = Resultado.objects.get(vigente=True, fatura_lanc=vista)
    r2 = Resultado.objects.get(vigente=True, fatura_lanc=parcela)
    assert r1.status == r2.status == "CONCILIADO_RESSALVA"
    assert "só pelo valor" in r1.motivo and "20/07/2026" in r1.motivo and "1 parcela(s)" in r2.motivo


def test_nf_do_controle_casa_a_nota_pelo_numero_mesmo_sem_nome_e_com_total_diferente():
    c, vista, _ = _cenario()
    # A fatura diz "WIX COM"; a nota é de outro nome e o total da compra (com frete) difere do valor na fatura.
    controle.importar(planilha(_linha("NF 4321", 14, "Licença", 390, 384)), "c.xlsx")
    motor.executar(c.comp)
    r = Resultado.objects.get(vigente=True, fatura_lanc=vista)
    assert r.status == "CONCILIADO_RESSALVA" and r.sem_comprovante and "NF nº 4321 informada" in r.motivo
    imp = _enviar(c, ("a.xml", nfe(4321, "2026-07-14", "390.00", "EMPRESA SEM NOME PARECIDO")))
    assert imp.resumo["casadas"] == 1
    assert Resultado.objects.get(vigente=True, fatura_lanc=vista).status == "CONCILIADO"


def test_credito_de_compra_cancelada_fica_com_ressalva():
    c, _, _ = _cenario()
    credito = c.lanc(datetime(2026, 7, 20).date(), "-200.00", "ESTORNO LOJA", cartao=c.fase2)
    controle.importar(planilha(_linha("CANCELADA", 10, "Cadeira", 200, -200)), "c.xlsx")
    motor.executar(c.comp)
    r = Resultado.objects.get(vigente=True, fatura_lanc=credito)
    assert r.status == "CONCILIADO_RESSALVA" and "cancelada" in r.motivo


def test_envio_pela_tela(client):
    c, vista, _ = _cenario()
    resp = client.post(f"/notas/{c.comp.codigo}/controle/", {"controle": planilha(_linha("", 14, "Licença", 384, 384))})
    assert resp.status_code == 302
    assert Resultado.objects.get(vigente=True, fatura_lanc=vista).status == "CONCILIADO_RESSALVA"
    pagina = client.get(f"/notas/{c.comp.codigo}/").content.decode()
    assert "Controle de compras" in pagina and "1 compra(s)" in pagina
