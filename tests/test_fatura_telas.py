"""Tela de importação da fatura (upload, acompanhamento por HTMX, resultado)."""
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from conciliacao import tarefas
from conciliacao.fatura import servico
from conciliacao.models import Importacao, conta_padrao

pytestmark = pytest.mark.django_db

SETEMBRO = Path(__file__).parent / "fixtures" / "fatura-2026-09.pdf"
IP = "10.0.0.8"


@pytest.fixture
def fila(monkeypatch):
    """Em vez de mandar para o RQ, guarda o id (e permite processar na hora)."""
    enviados = []
    monkeypatch.setattr(tarefas, "enfileirar_fatura", lambda pk: enviados.append(pk))
    return enviados


def test_pagina_de_importacao_abre(client):
    resp = client.get("/fatura/", REMOTE_ADDR=IP)
    assert resp.status_code == 200 and "Importar fatura" in resp.content.decode()


def test_inicio_vai_para_o_painel(client):
    resp = client.get("/", REMOTE_ADDR=IP)
    assert resp.status_code == 302 and resp["Location"].endswith("/painel/")


def test_upload_que_nao_e_pdf_mostra_erro(client, fila):
    arquivo = SimpleUploadedFile("fatura.pdf", b"nao sou pdf", content_type="application/pdf")
    resp = client.post("/fatura/", {"arquivo": arquivo, "conta": conta_padrao()}, REMOTE_ADDR=IP, follow=True)
    assert "não é um PDF válido" in resp.content.decode()
    assert fila == [] and not Importacao.objects.exists()


def test_upload_enfileira_e_mostra_acompanhamento(client, fila):
    if not SETEMBRO.exists():
        pytest.skip("fixture ausente")
    arquivo = SimpleUploadedFile("setembro.pdf", SETEMBRO.read_bytes(), content_type="application/pdf")
    resp = client.post("/fatura/", {"arquivo": arquivo, "conta": conta_padrao()}, REMOTE_ADDR=IP)
    imp = Importacao.objects.get()
    assert resp.status_code == 302 and resp["Location"].endswith(f"/fatura/{imp.pk}/")
    assert fila == [imp.pk] and imp.ip == IP and imp.status == "PROCESSANDO"

    pagina = client.get(f"/fatura/{imp.pk}/", REMOTE_ADDR=IP).content.decode()
    assert "Lendo a fatura" in pagina and 'hx-trigger="every 2s"' in pagina

    servico.processar(imp.pk)
    parcial = client.get(f"/fatura/{imp.pk}/parcial/", REMOTE_ADDR=IP).content.decode()
    assert "hx-trigger" not in parcial          # terminou: para de consultar
    assert "16 de 16" in parcial and "R$ 257.509,11" in parcial and "em uso" in parcial

    secao = imp.secoes.exclude(cartao=None).first()
    detalhe = client.get(f"/fatura/{imp.pk}/?secao={secao.pk}", REMOTE_ADDR=IP).content.decode()
    assert "Lançamentos ·" in detalhe

    historico = client.get("/fatura/", REMOTE_ADDR=IP).content.decode()
    assert IP in historico and "em uso" in historico


def test_mesmo_arquivo_avisa_e_nao_enfileira(client, fila):
    if not SETEMBRO.exists():
        pytest.skip("fixture ausente")
    imp, _ = servico.criar_importacao(SETEMBRO, SETEMBRO.name)
    servico.processar(imp.pk)
    arquivo = SimpleUploadedFile("setembro.pdf", SETEMBRO.read_bytes(), content_type="application/pdf")
    resp = client.post("/fatura/", {"arquivo": arquivo, "conta": conta_padrao()}, REMOTE_ADDR=IP, follow=True)
    assert "mesmo da versão em uso" in resp.content.decode()
    assert fila == [] and Importacao.objects.count() == 1


def test_historico_com_fatura_que_falhou_antes_de_ter_competencia(client):
    # Leitura que falha antes do resumo: sem competência e (importação antiga) sem banco.
    Importacao.objects.create(tipo="FATURA", origem="UPLOAD", status="ERRO", arquivo_nome_original="ruim.pdf",
                              resumo={"erro": "Nenhuma página com a tabela de lançamentos"})
    pagina = client.get("/fatura/", REMOTE_ADDR=IP)
    assert pagina.status_code == 200 and "ruim.pdf" in pagina.content.decode()
