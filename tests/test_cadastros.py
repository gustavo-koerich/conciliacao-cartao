"""Parâmetros, admin, auditoria e carga de cadastro."""
import json

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

from conciliacao import auditoria, parametros
from conciliacao.models import Auditoria, Cartao, Parametro, Representante

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client):
    usuario = get_user_model().objects.create_superuser("admin-teste", password=None)
    client.force_login(usuario)
    return client


def test_migration_grava_todos_os_parametros_do_catalogo():
    snapshot = parametros.obter_todos()
    assert set(snapshot) == set(parametros.CATALOGO)
    assert snapshot["janela_dias"] == 3
    assert snapshot["tolerancia_parcela_reais"] == "0.05"
    assert snapshot["de_para_medicos"]["sem medico vinculado"] == "SEM MÉDICO"


def test_parametro_faltando_impede_execucao():
    Parametro.objects.filter(chave="janela_dias").delete()
    with pytest.raises(parametros.ParametroInvalido, match="janela_dias"):
        parametros.obter_todos()


@pytest.mark.parametrize("chave,valor", [
    ("janela_dias", -1), ("janela_dias", "3"), ("janela_dias", True), ("tolerancia_parcela_reais", "abc"),
    ("de_para_medicos", ["lista"]), ("palavras_categoria", {"Alimentação": "texto"}),
])
def test_validacao_recusa_tipo_errado(chave, valor):
    with pytest.raises(parametros.ParametroInvalido):
        parametros.validar(chave, valor)


def test_de_para_normaliza_chaves():
    assert parametros.validar("de_para_medicos", {"  Vários   Médicos ": "VÁRIOS"}) == {"varios medicos": "VÁRIOS"}


def test_admin_edita_parametro_e_audita(admin_client):
    p = Parametro.objects.get(chave="janela_dias")
    resp = admin_client.post(f"/admin/conciliacao/parametro/{p.pk}/change/", {"valor": "5"}, REMOTE_ADDR="10.0.0.7")
    assert resp.status_code == 302
    p.refresh_from_db()
    assert p.valor == 5 and p.atualizado_por == "admin-teste"
    registro = Auditoria.objects.get(acao="CADASTRO")
    assert registro.ip == "10.0.0.7" and registro.detalhe["campos"] == ["valor"]


def test_admin_recusa_parametro_invalido(admin_client):
    p = Parametro.objects.get(chave="janela_dias")
    resp = admin_client.post(f"/admin/conciliacao/parametro/{p.pk}/change/", {"valor": "{ruim"}, REMOTE_ADDR="10.0.0.7")
    assert resp.status_code == 200 and "JSON inválido" in resp.content.decode()
    p.refresh_from_db()
    assert p.valor == 3


@pytest.mark.parametrize("pagina", ["", "conciliacao/representante/", "conciliacao/cartao/", "conciliacao/parametro/",
                                    "conciliacao/competencia/", "conciliacao/importacao/", "conciliacao/execucao/",
                                    "conciliacao/auditoria/", "conciliacao/backup/"])
def test_paginas_do_admin_abrem(admin_client, pagina):
    assert admin_client.get(f"/admin/{pagina}", REMOTE_ADDR="10.0.0.7").status_code == 200


def test_admin_nao_permite_apagar(admin_client):
    rep = Representante.objects.create(nome="Teste")
    resp = admin_client.get(f"/admin/conciliacao/representante/{rep.pk}/delete/", REMOTE_ADDR="10.0.0.7")
    assert resp.status_code == 403


def test_auditoria_sem_request_grava_usuario_informado():
    registro = auditoria.registrar(None, "BACKUP", "OK", usuario="servico:backup", detalhe={"arquivo": "x.age"})
    assert registro.usuario == "servico:backup" and registro.ip is None


def _csv(tmp_path, linhas):
    caminho = tmp_path / "cadastro.csv"
    caminho.write_text("final;nome_fatura;representante;email;sheet_id;escopo\n" + "\n".join(linhas) + "\n",
                       encoding="utf-8")
    return caminho


def test_carregar_cadastro_e_idempotente(tmp_path):
    arquivo = _csv(tmp_path, ["5501;FABIO LUIZ N SALES;Fábio;;;CONCILIAR_FORMULARIO",
                              "5507;JOAO C SILVEIRA PRADO;João;;;FASE2_NF"])
    call_command("carregar_cadastro", str(arquivo))
    call_command("carregar_cadastro", str(arquivo))
    assert Representante.objects.count() == 2
    assert Cartao.objects.get(final="5507").escopo == "FASE2_NF"
    ultimas = Auditoria.objects.filter(acao="CADASTRO").order_by("-id")
    assert ultimas[0].detalhe["cartoes_novos"] == 0 and ultimas[1].detalhe["cartoes_novos"] == 2


def test_carregar_cadastro_nao_apaga_email_com_campo_vazio(tmp_path):
    call_command("carregar_cadastro", str(_csv(tmp_path, ["5501;FABIO;Fábio;fabio@exemplo.com;;"])))
    call_command("carregar_cadastro", str(_csv(tmp_path, ["5501;FABIO;Fábio;;;"])))
    assert Representante.objects.get(nome="Fábio").email == "fabio@exemplo.com"


def test_carregar_cadastro_completa_pendente(tmp_path):
    Cartao.objects.create(final="9999", nome_fatura="NOVO PORTADOR", pendente_cadastro=True)
    call_command("carregar_cadastro", str(_csv(tmp_path, ["9999;NOVO PORTADOR;Fulano;;;"])))
    assert not Cartao.objects.get(final="9999").pendente_cadastro


def test_carregar_cadastro_recusa_escopo_invalido_sem_gravar_nada(tmp_path):
    arquivo = _csv(tmp_path, ["5501;FABIO;Fábio;;;CONCILIAR_FORMULARIO", "1234;X;Y;;;INVENTADO"])
    with pytest.raises(CommandError, match="escopo inválido"):
        call_command("carregar_cadastro", str(arquivo))
    assert not Cartao.objects.exists()


def test_carregar_cadastro_recusa_cabecalho_errado(tmp_path):
    caminho = tmp_path / "x.csv"
    caminho.write_text(json.dumps({"a": 1}), encoding="utf-8")
    with pytest.raises(CommandError, match="Cabeçalho"):
        call_command("carregar_cadastro", str(caminho))
