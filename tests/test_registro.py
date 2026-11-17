"""Registro de alterações da Conciliação: só administradores da Central veem; filtros por usuário e ação."""
import pytest

from conciliacao import auditoria
from tests.test_acesso_central import COOKIE, RespostaFalsa, central, modo_central  # noqa: F401

pytestmark = pytest.mark.django_db


def _abrir(client, central, admin, query=""):
    central["resposta"] = RespostaFalsa(200, {"usuario": "ana" if admin else "bia", "nome": "X", "admin": admin,
                                              "permitido": True})
    client.cookies[COOKIE] = "adm" if admin else "comum"
    return client.get(f"/registro/{query}", REMOTE_ADDR="10.0.0.9")


def test_admin_ve_o_registro_com_filtros(client, central):
    auditoria.registrar(None, "AJUSTE_MANUAL", "OK", usuario="suelen", ip="10.0.0.5", detalhe={"status": "CONCILIADO"})
    auditoria.registrar(None, "EXPORTACAO", "OK", usuario="fabio", ip="10.0.0.6", detalhe={"linhas": 12})
    pagina = _abrir(client, central, True).content.decode()
    assert "Registro de alterações" in pagina and "suelen" in pagina and "fabio" in pagina and "status: CONCILIADO" in pagina
    pagina = _abrir(client, central, True, "?usuario=suelen&acao=AJUSTE_MANUAL").content.decode()
    assert "status: CONCILIADO" in pagina and "linhas: 12" not in pagina


def test_quem_nao_e_admin_nao_ve(client, central):
    resp = _abrir(client, central, False)
    assert resp.status_code == 403 and "administradores" in resp.content.decode()
    assert 'href="/registro/"' not in client.get("/fatura/", REMOTE_ADDR="10.0.0.9").content.decode()
