"""Exportação da base: uma tabela por CSV, dicionário de dados, paciente mascarado."""
import io
import zipfile
from datetime import date

import pytest
from django.core.management import call_command

from conciliacao import exportacao

pytestmark = pytest.mark.django_db


def test_exporta_todas_as_tabelas_com_dicionario_e_mascara(client, tmp_path):
    from tests.test_motor import Cenario
    c = Cenario()
    c.form(date(2026, 7, 10), "50.00", paciente="MARIA DA SILVA")
    resp = client.get("/cadastros/exportar/")
    assert resp.status_code == 200 and resp["Content-Type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    nomes = set(zf.namelist())
    assert {"dicionario.csv", "LEIA-ME.txt"} <= nomes
    assert {f"tabelas/{m._meta.db_table}.csv" for m in exportacao.modelos()} <= nomes
    dic = zf.read("dicionario.csv").decode("utf-8-sig")
    assert "conc_cartao;representante_id;ForeignKey" in dic
    assert "MARIA DA SILVA" not in b"".join(zf.read(n) for n in nomes).decode("utf-8", "ignore")
    saida = tmp_path / "x.zip"
    call_command("exportar_dados", saida=str(saida))
    assert zipfile.is_zipfile(saida)
