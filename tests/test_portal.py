"""Portal de pendências: link do representante, respostas pelo celular, laranja até aprovar, segurança do link."""
import re
from datetime import date, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from conciliacao import analise, pendencias
from conciliacao.models import Anexo, LinkPendencias, RespostaPendencia, Resultado
from tests.test_motor import Cenario

pytestmark = pytest.mark.django_db
IP = "10.0.0.8"
JPG = b"\xff\xd8\xff\xe0" + b"0" * 200


@pytest.fixture(autouse=True)
def portal(settings):
    settings.PORTA_PORTAL = "8002"
    settings.PORTAL_URL_BASE = "https://servidor.ts.net:8443"


def _cenario():
    c = Cenario()
    l1 = c.lanc(date(2026, 7, 9), "48.00", "RESTAURANTE BOM", )
    l2 = c.lanc(date(2026, 7, 12), "35.00", "ESTACIONAMENTO")
    c.rodar()
    return c, l1, l2


def _gerar(client, c, dias=7):
    resp = client.post(f"/painel/{c.comp.codigo}/representante/{c.rep.pk}/link/", {"dias": str(dias)}, REMOTE_ADDR=IP)
    url = re.search(r"https://servidor\.ts\.net:8443/pendencias/([A-Za-z0-9_\-]+)/", resp.content.decode())
    return resp, url.group(1)


def portal_get(client, caminho):
    return client.get(caminho, SERVER_PORT="8002", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="200.10.20.30")


def portal_post(client, caminho, dados):
    return client.post(caminho, dados, SERVER_PORT="8002", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="200.10.20.30")


def test_gerar_link_mostra_mensagem_e_guarda_so_a_impressao(client):
    c, l1, l2 = _cenario()
    resp, token = _gerar(client, c)
    pagina = resp.content.decode()
    assert "Abrir no WhatsApp" in pagina and "wa.me/?text=" in pagina and "2 gasto(s)" in pagina
    link = LinkPendencias.objects.get()
    assert link.token_hash == pendencias._hash(token) and token not in str(link.__dict__)
    assert (link.expira_em - timezone.now()).days in (6, 7) and len(link.chaves) == 2
    assert client.post(f"/painel/{c.comp.codigo}/representante/{c.rep.pk}/link/", {"dias": "99"},
                       REMOTE_ADDR=IP, follow=True).content.decode().count("Validade inválida") == 1


def test_portal_abre_sem_login_so_em_pendencias_e_sem_cache(client):
    c, l1, l2 = _cenario()
    _, token = _gerar(client, c)
    resp = portal_get(client, f"/pendencias/{token}/")
    pagina = resp.content.decode()
    assert resp.status_code == 200 and "RESTAURANTE BOM" in pagina and "Tirar foto do comprovante" in pagina
    assert resp["Cache-Control"] == "no-store" and resp["Referrer-Policy"] == "same-origin"
    assert "noindex" in resp["X-Robots-Tag"]
    assert portal_get(client, f"/painel/{c.comp.codigo}/").status_code == 404     # o resto do sistema não existe aqui
    assert portal_get(client, "/fatura/").status_code == 404
    assert portal_get(client, "/pendencias/codigo-que-nao-existe/").status_code == 404
    link = LinkPendencias.objects.get()
    assert link.aberto_em is not None and link.acessos == 1


def test_resposta_fica_laranja_e_aprovar_deixa_verde(client):
    c, l1, l2 = _cenario()
    _, token = _gerar(client, c)
    foto = SimpleUploadedFile("recibo.jpg", JPG, content_type="image/jpeg")
    resp = portal_post(client, f"/pendencias/{token}/1/", {
        "categoria": "Alimentação com médicos", "descricao": "almoço pós-procedimento", "medico": "Dr. A",
        "paciente": "Fulano", "cidade": "SP", "comprovante": foto})
    assert resp.status_code == 302 and "salvo=1" in resp["Location"]
    r = Resultado.objects.get(vigente=True, fatura_lanc=l1)
    assert r.status == "AGUARDANDO_APROVACAO" and "aguardando aprovação" in r.motivo
    assert Anexo.objects.filter(nome_original="recibo.jpg", usuario__startswith="representante").exists()
    assert "Enviado, aguardando o financeiro" in portal_get(client, f"/pendencias/{token}/").content.decode()
    tela = client.get(f"/painel/{c.comp.codigo}/representante/{c.rep.pk}/", REMOTE_ADDR=IP).content.decode()
    assert "linha-laranja" in tela and "Resposta do representante" in tela and "Aprovar" in tela
    client.post(f"/resultado/{r.pk}/aprovar/", REMOTE_ADDR=IP)
    r = Resultado.objects.get(vigente=True, fatura_lanc=l1)
    assert r.status == "CONCILIADO" and "aprovado" in r.motivo
    pagina = portal_get(client, f"/pendencias/{token}/").content.decode()
    assert "Conferido" in pagina
    assert portal_post(client, f"/pendencias/{token}/1/", {"categoria": "Outros", "descricao": "x"}).status_code == 400
    # na análise a linha é da fatura (valor faturado), com o que o representante respondeu pelo link
    linhas = [l for l in analise.coletar()["linhas"] if l.get("origem") == "Respondido pelo link"]
    assert [(l["fonte"], l["categoria"], l["valor"]) for l in linhas] == [("Fatura", "Alimentação com médico", l1.valor)]


def test_recusar_volta_a_pendente_com_motivo_e_pode_responder_de_novo(client):
    c, l1, _ = _cenario()
    _, token = _gerar(client, c)
    portal_post(client, f"/pendencias/{token}/1/", {"categoria": "Alimentação pessoal", "descricao": "almoço"})
    r = Resultado.objects.get(vigente=True, fatura_lanc=l1)
    client.post(f"/resultado/{r.pk}/recusar/", {"motivo": "falta a foto do recibo"}, REMOTE_ADDR=IP)
    r = Resultado.objects.get(vigente=True, fatura_lanc=l1)
    assert r.status == "SEM_FORMULARIO" and "falta a foto do recibo" in r.motivo
    pagina = portal_get(client, f"/pendencias/{token}/").content.decode()
    assert "O financeiro pediu correção: falta a foto do recibo" in pagina
    portal_post(client, f"/pendencias/{token}/1/", {"categoria": "Alimentação pessoal", "descricao": "almoço",
                                                    "comprovante": SimpleUploadedFile("r.jpg", JPG)})
    assert Resultado.objects.get(vigente=True, fatura_lanc=l1).status == "AGUARDANDO_APROVACAO"
    assert RespostaPendencia.objects.get().recusado_em is None


def test_resposta_pelo_celular_passa_na_protecao_csrf(client, settings):
    from django.test import Client
    settings.CSRF_COOKIE_PATH = "/conciliacao"
    settings.CSRF_TRUSTED_ORIGINS = [settings.PORTAL_URL_BASE]
    c, l1, _ = _cenario()
    _, token = _gerar(client, c)
    celular = Client(enforce_csrf_checks=True)
    extra = {"SERVER_PORT": "8002", "REMOTE_ADDR": "127.0.0.1", "secure": True}
    pagina = celular.get(f"/pendencias/{token}/", **extra)
    assert pagina["Referrer-Policy"] == "same-origin" and "no-referrer" not in pagina.content.decode()
    assert pagina.cookies[settings.CSRF_COOKIE_NAME]["path"] == "/pendencias/"
    csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', pagina.content.decode()).group(1)
    dados = {"csrfmiddlewaretoken": csrf, "categoria": "Outros", "descricao": "táxi"}
    assert celular.post(f"/pendencias/{token}/1/", dados, HTTP_ORIGIN="null", **extra).status_code == 403
    resp = celular.post(f"/pendencias/{token}/1/", dados, HTTP_ORIGIN=settings.PORTAL_URL_BASE, **extra)
    assert resp.status_code == 302
    assert Resultado.objects.get(vigente=True, fatura_lanc=l1).status == "AGUARDANDO_APROVACAO"


def test_nao_reconheco_vai_para_analise(client):
    c, l1, _ = _cenario()
    _, token = _gerar(client, c)
    portal_post(client, f"/pendencias/{token}/1/", {"nao_reconhece": "1", "observacao": "não estava nessa cidade"})
    r = Resultado.objects.get(vigente=True, fatura_lanc=l1)
    assert r.status == "PENDENTE_ANALISE" and "não reconhece" in r.motivo


def test_validacoes_da_resposta(client):
    c, _, _ = _cenario()
    _, token = _gerar(client, c)
    resp = portal_post(client, f"/pendencias/{token}/1/", {"categoria": "", "descricao": "x"})
    assert resp.status_code == 400 and "Escolha a categoria" in resp.content.decode()
    resp = portal_post(client, f"/pendencias/{token}/1/", {"categoria": "Outros", "descricao": ""})
    assert resp.status_code == 400 and "Descreva o gasto" in resp.content.decode()
    resp = portal_post(client, f"/pendencias/{token}/1/", {"categoria": "Outros", "descricao": "x",
                                                           "comprovante": SimpleUploadedFile("a.exe", b"MZ" * 50)})
    assert resp.status_code == 400 and not RespostaPendencia.objects.exists()
    assert portal_post(client, f"/pendencias/{token}/99/", {"categoria": "Outros", "descricao": "x"}).status_code == 400


def _como(monkeypatch, settings, client, admin):
    from conciliacao import middleware
    from tests.test_acesso_central import COOKIE, RespostaFalsa
    settings.ACESSO_MODO, settings.CENTRAL_LOGIN_URL = "central", "/login"
    monkeypatch.setattr(middleware.requests, "get", lambda *a, **k: RespostaFalsa(200, {
        "usuario": "ana" if admin else "bia", "nome": "X", "admin": admin, "permitido": True}))
    client.cookies[COOKIE] = "adm" if admin else "comum"


def test_excluir_teste_apaga_tudo_e_deixa_uma_linha_no_registro(client, settings, tmp_path, monkeypatch):
    from pathlib import Path
    from conciliacao.models import Auditoria
    settings.ARQUIVOS_DIR = str(tmp_path)
    c, l1, _ = _cenario()
    _, token = _gerar(client, c)
    portal_post(client, f"/pendencias/{token}/1/", {"categoria": "Outros", "descricao": "Teste",
                                                    "comprovante": SimpleUploadedFile("image.jpg", JPG)})
    r = Resultado.objects.get(vigente=True, fatura_lanc=l1)
    client.post(f"/resultado/{r.pk}/aprovar/", REMOTE_ADDR=IP)
    arquivo = Path(Anexo.objects.get().arquivo_caminho)
    assert arquivo.exists() and Auditoria.objects.filter(acao__in=["ANEXO", "CADASTRO", "AJUSTE_MANUAL"]).count() >= 3
    link = LinkPendencias.objects.get()
    _como(monkeypatch, settings, client, admin=True)
    client.post(f"/link-pendencias/{link.pk}/excluir-teste/", REMOTE_ADDR=IP)
    assert not LinkPendencias.objects.exists() and not RespostaPendencia.objects.exists() and not Anexo.objects.exists()
    assert not arquivo.exists()
    assert Resultado.objects.get(vigente=True, fatura_lanc=l1).status == "SEM_FORMULARIO"
    sobra = list(Auditoria.objects.filter(acao__in=["ANEXO", "CADASTRO", "AJUSTE_MANUAL", "EXCLUSAO_TESTE"]))
    assert [a.acao for a in sobra] == ["EXCLUSAO_TESTE"] and sobra[0].detalhe["comprovantes"] == 1
    assert portal_get(client, f"/pendencias/{token}/").status_code == 404


def test_excluir_teste_so_para_administrador(client, settings, monkeypatch):
    c, _, _ = _cenario()
    _gerar(client, c)
    link = LinkPendencias.objects.get()
    _como(monkeypatch, settings, client, admin=False)
    assert client.post(f"/link-pendencias/{link.pk}/excluir-teste/", REMOTE_ADDR=IP).status_code == 403
    assert LinkPendencias.objects.exists()


def test_link_vencido_ou_revogado_nao_abre(client):
    c, _, _ = _cenario()
    _, token = _gerar(client, c)
    link = LinkPendencias.objects.get()
    client.post(f"/link-pendencias/{link.pk}/revogar/", REMOTE_ADDR=IP)
    resp = portal_get(client, f"/pendencias/{token}/")
    assert resp.status_code == 410 and "cancelado" in resp.content.decode()
    _, token2 = _gerar(client, c)
    LinkPendencias.objects.filter(revogado_em__isnull=True).update(expira_em=timezone.now() - timedelta(minutes=1))
    resp = portal_post(client, f"/pendencias/{token2}/1/", {"categoria": "Outros", "descricao": "x"})
    assert resp.status_code == 410 and "venceu" in resp.content.decode()
