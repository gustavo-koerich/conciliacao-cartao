"""Avisos de conferência (só leitura) e código do link fora dos logs (relatório de auditoria, 26/10/2026)."""
import json
import logging
from datetime import date
from decimal import Decimal as D

import pytest

from conciliacao import avisos
from conciliacao.logs import FormatadorJson, mascarar
from conciliacao.models import (AjusteManual, Competencia, FaturaLanc, Importacao, Resultado, chave_lancamento)
from tests.test_motor import Cenario

pytestmark = pytest.mark.django_db
IP = "10.0.0.8"


def test_compra_repetida_na_mesma_fatura_e_avisada_sem_mudar_nada(client):
    c = Cenario()
    a = c.lanc(date(2026, 7, 16), "5.00", "UBER")
    b = c.lanc(date(2026, 7, 16), "5.00", "UBER")
    unica = c.lanc(date(2026, 7, 17), "5.00", "UBER")
    c.rodar()
    assert avisos.cobrancas_repetidas(c.fatura) == {a.pk: 2, b.pk: 2}
    antes = list(Resultado.objects.filter(vigente=True).values_list("pk", "status"))
    pagina = client.get(f"/painel/{c.comp.codigo}/representante/{c.rep.pk}/", REMOTE_ADDR=IP).content.decode()
    assert pagina.count("possível cobrança em dobro (2×)") == 2
    painel = client.get(f"/painel/{c.comp.codigo}/", REMOTE_ADDR=IP).content.decode()
    assert "<strong>2</strong> lançamentos repetidos na fatura" in painel
    assert list(Resultado.objects.filter(vigente=True).values_list("pk", "status")) == antes   # só leitura
    assert unica.pk not in avisos.cobrancas_repetidas(c.fatura)


def test_credito_sem_compra_sugere_a_compra_da_fatura_anterior(client):
    c = Cenario()
    compra = c.lanc(date(2026, 7, 10), "300.00", "Tkt Aereo")
    c.rodar()
    set_ = Competencia.objects.create(ano_mes="2026-09", periodo_ini=date(2026, 8, 3), periodo_fim=date(2026, 9, 1),
                                      fechamento=date(2026, 9, 1), vencimento=date(2026, 9, 11), total_fatura=D("-300"))
    fat = Importacao.objects.create(tipo="FATURA", competencia=set_, versao=1, origem="UPLOAD", status="OK", ativo=True)
    Importacao.objects.create(tipo="PLANILHA", representante=c.rep, competencia=set_, versao=1, origem="UPLOAD",
                              status="OK", ativo=True)
    credito = FaturaLanc.objects.create(importacao=fat, cartao=c.cartao, secao="PORTADOR", natureza="CREDITO",
                                        data=date(2026, 8, 20), descricao="Tkt Aereo", valor=D("-300.00"), ordem=1)
    from conciliacao import motor
    motor.executar(set_)
    assert Resultado.objects.get(vigente=True, fatura_lanc=credito).status == "PENDENTE_ANALISE"
    assert avisos.estornos_provaveis([credito]) == {credito.pk: compra}
    pagina = client.get(f"/painel/{set_.codigo}/representante/{c.rep.pk}/", REMOTE_ADDR=IP).content.decode()
    assert "provável estorno da compra de 10/07/2026" in pagina


def test_edicao_solta_depois_de_trocar_a_versao_aparece_no_painel(client):
    c = Cenario()
    lanc = c.lanc(date(2026, 7, 10), "80.00", "HOTEL X")
    c.rodar()
    AjusteManual.objects.create(competencia=c.comp, chave=chave_lancamento(lanc), status="CONCILIADO", usuario="s")
    assert avisos.edicoes_soltas(c.comp, c.fatura) == []
    # nova leitura com a descrição corrigida: a edição antiga não acha mais o lançamento
    FaturaLanc.objects.filter(pk=lanc.pk).update(descricao="HOTEL XY")
    soltas = avisos.edicoes_soltas(c.comp, c.fatura)
    assert soltas and soltas[0][0] == "situação ajustada à mão"
    painel = client.get(f"/painel/{c.comp.codigo}/", REMOTE_ADDR=IP).content.decode()
    assert "1 edição(ões) ficaram sem lançamento" in painel and "HOTEL X" in painel
    assert AjusteManual.objects.filter(ativo=True).count() == 1                              # nada apagado


def test_codigo_do_link_nunca_vai_para_o_log():
    assert mascarar("GET /pendencias/AbC_123-x/1/ HTTP/1.1") == "GET /pendencias/***/1/ HTTP/1.1"
    assert mascarar("<WSGIRequest: POST '/conciliacao/pendencias/Tok3n/'>") == "<WSGIRequest: POST '/conciliacao/pendencias/***/'>"
    registro = logging.LogRecord("django.security.csrf", logging.WARNING, "", 0,
                                 "Forbidden: /conciliacao/pendencias/SEGREDO123/1/", (), None)
    registro.request = "<WSGIRequest: POST '/conciliacao/pendencias/SEGREDO123/1/'>"
    linha = FormatadorJson().format(registro)
    assert "SEGREDO123" not in linha and json.loads(linha)["msg"] == "Forbidden: /conciliacao/pendencias/***/1/"


def test_log_de_acesso_do_gunicorn_mascara_o_codigo():
    from types import SimpleNamespace
    from config.gunicorn_log import LogSemCodigo
    from gunicorn.config import Config
    log = LogSemCodigo(Config())
    req = SimpleNamespace(method="GET", uri="/pendencias/SEGREDO/", path="/pendencias/SEGREDO/", query="",
                          version=(1, 1), headers=[])
    resp = SimpleNamespace(status="200 OK", sent=10, headers=[], response_length=10)
    from datetime import timedelta
    ambiente = {"REMOTE_ADDR": "1.2.3.4", "RAW_URI": "/pendencias/SEGREDO/", "REQUEST_METHOD": "GET",
                "PATH_INFO": "/pendencias/SEGREDO/", "QUERY_STRING": "", "SERVER_PROTOCOL": "HTTP/1.1",
                "HTTP_REFERER": "https://x/pendencias/SEGREDO/", "HTTP_USER_AGENT": "teste"}
    atomos = log.atoms(resp, req, ambiente, timedelta(seconds=1))
    assert "SEGREDO" not in atomos["r"] and "SEGREDO" not in atomos["U"] and "SEGREDO" not in atomos["f"]
