"""Link da internet (Cloudflare Tunnel + Access): porta externa exige o token assinado do Access."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from conciliacao import externo
from tests.test_acesso_central import COOKIE, RespostaFalsa, central, modo_central  # noqa: F401

pytestmark = pytest.mark.django_db

CHAVE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TIME, AUD = "exemplo", "aud-de-teste"


class _Jwks:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": CHAVE.public_key()})()


@pytest.fixture(autouse=True)
def access(settings, monkeypatch):
    settings.CF_ACCESS_TIME, settings.CF_ACCESS_AUD, settings.PORTA_EXTERNA = TIME, AUD, "8001"
    monkeypatch.setattr(externo, "_cliente_jwks", lambda: _Jwks())


def token(aud=AUD, iss=f"https://{TIME}.cloudflareaccess.com", chave=CHAVE, exp=300):
    agora = int(time.time())
    return jwt.encode({"aud": [aud], "iss": iss, "iat": agora, "exp": agora + exp, "email": "pessoa@exemplo.com"},
                      chave, algorithm="RS256")


def get(client, tok=None, caminho="/fatura/", porta="8001", ip="200.150.10.20"):
    client.cookies[COOKIE] = "cookie-assinado"
    extra = {"HTTP_CF_CONNECTING_IP": ip}
    if tok is not None:
        extra["HTTP_CF_ACCESS_JWT_ASSERTION"] = tok
    return client.get(caminho, SERVER_PORT=porta, REMOTE_ADDR="172.18.0.9", **extra)


def test_token_valido_passa_mesmo_com_ip_publico_e_avisa_a_central(client, central):
    central["resposta"] = RespostaFalsa(200, {"usuario": "ana", "nome": "Ana", "permitido": True})
    assert get(client, token()).status_code == 200
    assert central["headers"] == {"X-Acesso-Externo": "1"}


@pytest.mark.parametrize("tok", [None, "", "lixo", "x.y.z"])
def test_sem_token_ou_token_invalido_e_recusado(client, central, tok):
    assert get(client, tok).status_code == 403 and central["chamadas"] == 0


def test_token_de_outro_app_ou_emissor_ou_chave_ou_vencido_e_recusado(client, central):
    outra = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for tok in (token(aud="outro"), token(iss="https://outro.cloudflareaccess.com"), token(chave=outra),
                token(exp=-10)):
        assert get(client, tok).status_code == 403
    assert central["chamadas"] == 0


def test_sem_configuracao_recusa_tudo(client, central, settings):
    settings.CF_ACCESS_AUD = ""
    assert get(client, token()).status_code == 403


def test_funnel_sem_token_passa_com_login_da_central_e_usa_o_ultimo_ip(client, central, settings):
    settings.ACESSO_EXTERNO = "funnel"
    central["resposta"] = RespostaFalsa(200, {"usuario": "ana", "nome": "Ana", "permitido": True})
    client.cookies[COOKIE] = "cookie-assinado"
    resp = client.get("/fatura/", SERVER_PORT="8001", REMOTE_ADDR="172.18.0.1",
                      HTTP_X_FORWARDED_FOR="10.0.0.1, 200.150.10.20")
    assert resp.status_code == 200 and central["headers"] == {"X-Acesso-Externo": "1"}
    assert resp.wsgi_request.ip_cliente == "200.150.10.20"
    central["resposta"] = RespostaFalsa(401, {})
    client.cookies[COOKIE] = "outra-sessao"
    assert client.get("/fatura/", SERVER_PORT="8001", HTTP_X_FORWARDED_FOR="200.1.1.1").status_code == 302
    assert client.get("/admin/", SERVER_PORT="8001").status_code == 403


def test_modo_desconhecido_recusa_tudo(client, central, settings):
    settings.ACESSO_EXTERNO = "desligado"
    assert get(client, token()).status_code == 403


def test_admin_do_django_so_pela_rede_interna(client, central):
    assert get(client, token(), caminho="/admin/").status_code == 403


def test_porta_interna_continua_como_antes(client, central):
    central["resposta"] = RespostaFalsa(200, {"usuario": "ana", "nome": "Ana", "permitido": True})
    client.cookies[COOKIE] = "cookie-assinado"
    resp = client.get("/fatura/", REMOTE_ADDR="10.0.0.9")
    assert resp.status_code == 200 and central["headers"] == {}


def test_cabecalho_host_nao_engana_a_porta(client, central):
    central["resposta"] = RespostaFalsa(200, {"usuario": "ana", "nome": "Ana", "permitido": True})
    client.cookies[COOKIE] = "cookie-assinado"
    resp = client.get("/fatura/", REMOTE_ADDR="10.0.0.9", HTTP_HOST="localhost:8001")
    assert resp.status_code == 200 and central["headers"] == {}             # interno: sem regra externa


def test_prefixo_retirado_no_wsgi(monkeypatch):
    import importlib
    monkeypatch.setenv("PREFIXO_URL", "/conciliacao")
    import config.wsgi as w
    w = importlib.reload(w)
    visto = {}
    monkeypatch.setattr(w, "_django", lambda env, sr: visto.setdefault("p", env["PATH_INFO"]))
    for entrada, saida in (("/conciliacao/fatura/", "/fatura/"), ("/conciliacao", "/"), ("/fatura/", "/fatura/"),
                           ("/conciliacaoX/", "/conciliacaoX/")):
        visto.clear()
        w.application({"PATH_INFO": entrada}, None)
        assert visto["p"] == saida
