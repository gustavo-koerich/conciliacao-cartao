"""Login pela Central de Automações (ACESSO_MODO=central)."""
import pytest
import requests
from django.contrib.auth import get_user_model

from conciliacao import middleware

pytestmark = pytest.mark.django_db

COOKIE = "central_sessao"


class RespostaFalsa:
    def __init__(self, status, dados=None):
        self.status_code = status
        self._dados = dados

    def json(self):
        return self._dados

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture(autouse=True)
def modo_central(settings):
    settings.ACESSO_MODO = "central"
    settings.CENTRAL_LOGIN_URL = "/login"


@pytest.fixture
def central(monkeypatch):
    """Substitui a chamada HTTP à Central e conta quantas vezes foi chamada."""
    estado = {"resposta": None, "chamadas": 0, "erro": None}

    def falso_get(url, params=None, cookies=None, timeout=None, allow_redirects=True, headers=None):
        estado["chamadas"] += 1
        estado["cookies"] = cookies
        estado["params"] = params
        estado["headers"] = headers or {}
        if estado["erro"]:
            raise estado["erro"]
        return estado["resposta"]

    monkeypatch.setattr(middleware.requests, "get", falso_get)
    return estado


def _get(client, **extra):
    client.cookies[COOKIE] = "cookie-assinado"
    return client.get("/fatura/", REMOTE_ADDR="10.0.0.9", **extra)


def test_sem_cookie_vai_para_o_login_da_central(client, central):
    resp = client.get("/fatura/", REMOTE_ADDR="10.0.0.9")
    assert resp.status_code == 302 and resp["Location"] == "/login"
    assert central["chamadas"] == 0


def test_sem_cookie_em_htmx_usa_hx_redirect(client, central):
    resp = client.get("/fatura/", REMOTE_ADDR="10.0.0.9", HTTP_HX_REQUEST="true")
    assert resp.status_code == 204 and resp["HX-Redirect"] == "/login"


def test_usuario_permitido_entra_e_vira_usuario_django(client, central):
    central["resposta"] = RespostaFalsa(200, {"usuario": "gustavo", "nome": "Gustavo", "admin": True, "permitido": True})
    resp = _get(client)
    assert resp.status_code == 200
    assert "Gustavo" in resp.content.decode()
    assert central["params"] == {"app": "conciliacao"}
    assert central["cookies"] == {COOKIE: "cookie-assinado"}
    u = get_user_model().objects.get(username="gustavo")
    assert u.is_staff and u.is_superuser and not u.has_usable_password()


def test_usuario_sem_permissao_recebe_403(client, central):
    central["resposta"] = RespostaFalsa(200, {"usuario": "maria", "nome": "Maria", "admin": False, "permitido": False})
    resp = _get(client)
    assert resp.status_code == 403
    assert not get_user_model().objects.filter(username="maria").exists()


def test_sessao_expirada_na_central_vai_para_o_login(client, central):
    central["resposta"] = RespostaFalsa(401)
    resp = _get(client)
    assert resp.status_code == 302 and resp["Location"] == "/login"


def test_central_fora_do_ar_falha_fechado(client, central):
    central["erro"] = requests.ConnectionError("sem rota")
    resp = _get(client)
    assert resp.status_code == 503


def test_resposta_estranha_da_central_falha_fechado(client, central):
    central["resposta"] = RespostaFalsa(200, ["nao", "e", "dict"])
    resp = _get(client)
    assert resp.status_code == 503


def test_validacao_fica_em_cache(client, central):
    central["resposta"] = RespostaFalsa(200, {"usuario": "ana", "nome": "Ana", "admin": False, "permitido": True})
    assert _get(client).status_code == 200
    assert _get(client).status_code == 200
    assert central["chamadas"] == 1


def test_perder_admin_na_central_tira_staff_no_django(client, central, settings):
    settings.CENTRAL_CACHE_SEGUNDOS = 0
    central["resposta"] = RespostaFalsa(200, {"usuario": "joao", "nome": "João", "admin": True, "permitido": True})
    _get(client)
    central["resposta"] = RespostaFalsa(200, {"usuario": "joao", "nome": "João", "admin": False, "permitido": True})
    _get(client)
    u = get_user_model().objects.get(username="joao")
    assert not u.is_staff and not u.is_superuser


def test_rede_continua_valendo_antes_da_central(client, central):
    client.cookies[COOKIE] = "cookie-assinado"
    resp = client.get("/fatura/", REMOTE_ADDR="8.8.8.8")
    assert resp.status_code == 403
    assert central["chamadas"] == 0


@pytest.mark.parametrize("dados,ve_link", [
    ({"admin": False, "outros_modulos": False}, False),   # só a Conciliação: não vê a Central
    ({"admin": False, "outros_modulos": True}, True),     # Conciliação + Licitações, por exemplo
    ({"admin": True}, True),
    ({"admin": False}, False),                            # Central antiga, sem o campo
])
def test_link_para_a_central_so_para_quem_tem_outros_modulos(client, central, dados, ve_link):
    central["resposta"] = RespostaFalsa(200, {"usuario": "ana", "nome": "Ana", "permitido": True} | dados)
    pagina = _get(client).content.decode()
    assert ('<a class="voltar" href="/">← Voltar à Central</a>' in pagina) is ve_link
    assert '<a class="voltar" href="/logout">Sair</a>' in pagina
