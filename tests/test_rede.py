"""Filtro de rede (ALLOWED_NETWORKS) e leitura segura do IP de origem."""
import pytest
from django.test import RequestFactory

from conciliacao.rede import ip_do_cliente, nome_maquina

pytestmark = pytest.mark.django_db


def test_ip_fora_das_redes_recebe_403(client):
    resp = client.get("/fatura/", REMOTE_ADDR="8.8.8.8")
    assert resp.status_code == 403
    assert "Acesso negado" in resp.content.decode()


def test_ip_dentro_das_redes_passa(client):
    resp = client.get("/fatura/", REMOTE_ADDR="10.1.2.3")
    assert resp.status_code == 200
    assert "10.1.2.3" in resp.content.decode()


def test_x_forwarded_for_de_proxy_confiavel_vale(client):
    # Tailscale Serve (127.0.0.1) repassando um cliente de fora das redes: deve barrar.
    resp = client.get("/fatura/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="8.8.8.8")
    assert resp.status_code == 403


def test_x_forwarded_for_forjado_por_cliente_comum_e_ignorado():
    req = RequestFactory().get("/", REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="100.100.1.1")
    assert ip_do_cliente(req) == "10.0.0.5"


def test_cadeia_x_forwarded_for_usa_o_ultimo_nao_confiavel():
    req = RequestFactory().get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="8.8.8.8, 100.100.1.2")
    assert ip_do_cliente(req) == "100.100.1.2"


def test_x_forwarded_for_malformado_cai_no_remote_addr():
    req = RequestFactory().get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="lixo")
    assert ip_do_cliente(req) == "127.0.0.1"


def test_healthz_livre_mesmo_fora_da_rede(client):
    resp = client.get("/healthz", REMOTE_ADDR="8.8.8.8")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_nome_maquina_ip_invalido_volta_vazio():
    assert nome_maquina("nao-e-ip") == ""
