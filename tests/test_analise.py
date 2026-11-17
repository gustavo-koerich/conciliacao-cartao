"""Análise de gastos: base geral, junção com as planilhas do sistema, lista geral, exportação e dashboard."""
import io
import json
from datetime import date, datetime
from decimal import Decimal

import openpyxl
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from conciliacao import analise, motor
from conciliacao.models import GastoBase, Importacao, Representante
from tests.test_motor import Cenario

pytestmark = pytest.mark.django_db
IP = "10.0.0.8"
CAB = ["Nome da Origem", "Mês", "Data", "Despesa", "Descrição", "\nFinalidade ", "\nPagamento ", "Médico", "Paciente",
       "Cidade", "Valor", "Ano", "Column11"]


def _geral(tmp_path, linhas, nome="geral.xlsx"):
    livro = openpyxl.Workbook()
    livro.active.title = "Dashboard"
    aba = livro.create_sheet("ATUALIZADOS")
    aba.append(CAB)
    for l in linhas:
        aba.append(l)
    caminho = tmp_path / nome
    livro.save(caminho)
    return caminho


def _l(nome, data, despesa, valor, pagamento="Cartão Crédito", medico="Dr X"):
    return [nome, "julho", data, despesa, "desc", "Negociação", pagamento, medico, "Paciente P", "São Paulo", valor,
            data.year if data else None, None]


LINHAS = [
    _l("FABIO", datetime(2026, 7, 2), "ALIMENTAÇÃO", 50.0),
    _l("FÁBIO", datetime(2026, 9, 10), "Alimentação com médico", 120.0),
    _l("JOSE VALENTE", datetime(2026, 7, 5), "COMBUSTÍVEL", 200.0),
    _l("JOSÉ", datetime(2026, 7, 6), "HOSPEDAGEM", 300.0),
    _l("HEITOR", datetime(2026, 7, 7), "BRINDES/PRESENTES", 40.0, pagamento="Reembolso"),
    _l(None, None, None, None),                                      # linha em branco
    _l("FABIO", datetime(2030, 1, 1), "ALIMENTAÇÃO", 10.0),          # data futura
]


def _importar(tmp_path, linhas=LINHAS, nome="geral.xlsx"):
    caminho = _geral(tmp_path, linhas, nome)
    return analise.importar_base(caminho, caminho.name, usuario="teste", ip=IP)


def test_importar_base_normaliza_nomes_e_categorias(tmp_path):
    jose = Representante.objects.create(nome="José")
    imp, criada = _importar(tmp_path)
    assert criada and imp.ativo and imp.resumo["linhas"] == 5
    assert imp.resumo["ignoradas"]["em branco"] == 1 and imp.resumo["ignoradas"]["data fora do período"] == 1
    assert imp.resumo["representantes_criados"] == ["Fábio", "Heitor"]
    g = {x.nome_origem + x.despesa_original: x for x in GastoBase.objects.all()}
    assert g["JOSE VALENTECOMBUSTÍVEL"].representante == jose and g["JOSÉHOSPEDAGEM"].representante == jose
    assert g["FABIOALIMENTAÇÃO"].representante == g["FÁBIOAlimentação com médico"].representante
    assert [g[k].categoria for k in ("FABIOALIMENTAÇÃO", "FÁBIOAlimentação com médico", "JOSÉHOSPEDAGEM",
                                     "HEITORBRINDES/PRESENTES")] == ["Alimentação", "Alimentação com médico",
                                                                       "Outros", "Brindes e presentes"]
    assert g["JOSÉHOSPEDAGEM"].finalidade == "Negociação" and g["JOSÉHOSPEDAGEM"].paciente == "Paciente P"


def test_mesmo_arquivo_nao_cria_versao_e_nova_versao_substitui(tmp_path):
    imp1, _ = _importar(tmp_path)
    mesmo, criada = analise.importar_base(tmp_path / "geral.xlsx", "geral.xlsx")
    assert not criada and mesmo.pk == imp1.pk
    imp2, criada = _importar(tmp_path, LINHAS[:2], nome="geral2.xlsx")
    assert criada and imp2.versao == 2 and not Importacao.objects.get(pk=imp1.pk).ativo
    assert len(analise.coletar()["linhas"]) == 2


def test_planilha_sem_colunas_e_recusada(tmp_path):
    livro = openpyxl.Workbook()
    livro.active.append(["x", "y"])
    livro.save(tmp_path / "x.xlsx")
    with pytest.raises(analise.BaseInvalida):
        analise.importar_base(tmp_path / "x.xlsx", "x.xlsx")
    assert not Importacao.objects.filter(tipo="BASE").exists()


def test_planilha_do_sistema_substitui_a_base_no_mesmo_periodo(tmp_path):
    c = Cenario()                                   # Fábio, planilha de 2026-08 (01/07 a 03/08) com uma resposta
    f = c.form(date(2026, 7, 2), "50.00", "Alimentação")
    lanc = c.lanc(date(2026, 7, 2), "50.00", "RESTAURANTE")
    motor.executar(c.comp)
    _importar(tmp_path)
    col = analise.coletar()
    fabio = [l for l in col["linhas"] if l["rep"] == "Fábio"]
    assert col["substituidas_pela_planilha"] == 1                      # 02/07 da base saiu
    # regra de 26/10/2026: a linha do sistema vem da FATURA (valor faturado), complementada pelo formulário
    assert [(l["fonte"], str(l["data"])) for l in fabio] == [("Fatura", "2026-07-02"), ("Base geral", "2026-09-10")]
    sistema = fabio[0]
    assert sistema["ref"] == f"L{lanc.pk}F{f.pk}" and sistema["situacao"] == "Conciliado"
    assert sistema["valor"] == lanc.valor and sistema["origem"] == "Formulário" and sistema["mes"] == c.comp.ano_mes


def test_exportacao_no_layout_da_geral(tmp_path):
    _importar(tmp_path)
    conteudo = analise.exportar_xlsx(analise.coletar()["linhas"])
    aba = openpyxl.load_workbook(io.BytesIO(conteudo))["ATUALIZADOS"]
    linhas = list(aba.iter_rows(values_only=True))
    assert list(linhas[0]) == analise.COLUNAS_EXPORTACAO
    assert len(linhas) == 6 and linhas[1][1] == "Julho" and linhas[1][8] == "Paciente P"


def test_telas_da_analise(client, tmp_path):
    assert "Ainda não há gastos" in client.get("/analise/", REMOTE_ADDR=IP).content.decode()
    arquivo = SimpleUploadedFile("geral.xlsx", _geral(tmp_path, LINHAS).read_bytes())
    resp = client.post("/analise/base/", {"arquivo": arquivo}, REMOTE_ADDR=IP, follow=True)
    assert "Base geral v1 em uso: 5 despesas" in resp.content.decode()
    assert Importacao.objects.get(tipo="BASE").ip == IP

    pagina = client.get("/analise/", REMOTE_ADDR=IP).content.decode()
    inicio = pagina.index('id="dados-analise"')
    dados = json.loads(pagina[pagina.index(">", inicio) + 1:pagina.index("</script>", inicio)])
    assert len(dados["linhas"]) == 5 and "Paciente P" not in pagina    # dashboard não leva paciente
    assert dados["categorias"][-1] == "Outros"

    # o período agora é digitado (MM/AAAA, mês da fatura) e começa no mês atual: abre o intervalo todo
    lista = client.get("/analise/lista/?rep=José&de=01/2020&ate=12/2030", REMOTE_ADDR=IP).content.decode()
    assert "<strong>2</strong> linhas" in lista and "Paciente P" in lista
    xlsx = client.get("/analise/lista/?categoria=Outros&de=01/2020&ate=12/2030&formato=xlsx", REMOTE_ADDR=IP)
    assert xlsx["Content-Type"].startswith("application/vnd.openxmlformats")
    assert len(list(openpyxl.load_workbook(io.BytesIO(xlsx.content))["ATUALIZADOS"].iter_rows())) == 2
