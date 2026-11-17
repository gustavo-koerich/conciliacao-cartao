"""Correção da categoria e da data preenchidas pelo representante; "Reembolso" em todas as listas de situação."""
from datetime import date

import pytest

from conciliacao import analise
from conciliacao.models import CorrecaoDespesa, FormDespesa, Resultado
from tests.test_motor import Cenario

pytestmark = pytest.mark.django_db
IP = "10.0.0.8"
URL = "/painel/ailos-2026-08/representante/{}/"


def _form_sozinho(c, **kw):
    c.lanc(date(2026, 7, 15), "1.00", "OUTRA LOJA")          # algo na fatura: o representante entra na conciliação
    f = c.form(date(2026, 7, 9), "84.00", **kw)
    c.rodar()
    return f, Resultado.objects.get(vigente=True, form_despesa=f, fatura_lanc__isnull=True)


def test_lista_de_categorias_na_tela(client):
    c = Cenario()
    _form_sozinho(c, despesa="Estacionamento")
    c.form(date(2026, 7, 10), "12.00", despesa="Alimentação")
    c.rodar()
    pagina = client.get(URL.format(c.rep.pk), REMOTE_ADDR=IP).content.decode()
    assert pagina.count('class="escolha-categoria"') == 3                     # 2 do formulário + 1 só da fatura
    assert '<option value="Estacionamento" selected>' in pagina                   # bate com a lista
    assert "informado: Alimentação" in pagina                                       # não bate: mostra o informado
    assert "Manutenção de Malas" in pagina and "Alimentação com médicos" in pagina


def test_corrigir_categoria_guarda_original_e_vale_na_analise(client):
    c = Cenario()
    f, r = _form_sozinho(c, despesa="Alimentação")
    resp = client.post(f"/resultado/{r.pk}/despesa/", {"campo": "CATEGORIA", "valor": "Alimentação com médicos"},
                       REMOTE_ADDR=IP)
    assert resp.status_code == 302
    corr = CorrecaoDespesa.objects.get(ativo=True)
    assert (corr.original, corr.novo) == ("Alimentação", "Alimentação com médicos")
    f.refresh_from_db()
    assert f.despesa == "Alimentação"                                              # original não é apagado
    assert "categoria corrigida" in client.get(URL.format(c.rep.pk), REMOTE_ADDR=IP).content.decode()
    linhas = analise.coletar()["linhas"]
    assert [l["categoria"] for l in linhas if l["ref"] == f"F{f.pk}"] == ["Alimentação com médico"]
    # escolher de novo a opção igual ao informado (ou "voltar ao informado") desfaz
    r = Resultado.objects.get(vigente=True, form_despesa=f)
    client.post(f"/resultado/{r.pk}/despesa/", {"campo": "CATEGORIA", "valor": "", "acao": "desfazer"}, REMOTE_ADDR=IP)
    assert not CorrecaoDespesa.objects.filter(ativo=True).exists()


def test_categoria_fora_da_lista_e_recusada(client):
    c = Cenario()
    _, r = _form_sozinho(c)
    resp = client.post(f"/resultado/{r.pk}/despesa/", {"campo": "CATEGORIA", "valor": "Qualquer coisa"},
                       REMOTE_ADDR=IP, follow=True)
    assert "Escolha uma categoria da lista" in resp.content.decode() and not CorrecaoDespesa.objects.exists()


def test_familia_da_categoria_nao_vira_ressalva():
    c = Cenario()
    l = c.lanc(date(2026, 7, 9), "84.00", "RESTAURANTE BOM")
    c.form(date(2026, 7, 9), "84.00", despesa="Alimentação pessoal")
    c.rodar()
    assert Resultado.objects.get(vigente=True, fatura_lanc=l).status == "CONCILIADO"


def test_corrigir_data_concilia_e_desfazer_volta(client):
    c = Cenario()
    l = c.lanc(date(2026, 7, 9), "84.00", "LOJA")
    f = c.form(date(2026, 7, 29), "84.00")                     # representante errou o dia: 20 dias de distância
    c.rodar()
    r = Resultado.objects.get(vigente=True, fatura_lanc=l)
    assert r.status == "CONCILIADO_RESSALVA" and "20 dias" in r.motivo
    client.post(f"/resultado/{r.pk}/despesa/", {"campo": "DATA", "valor": "2026-07-09", "motivo": "recibo"},
                REMOTE_ADDR=IP)
    r = Resultado.objects.get(vigente=True, fatura_lanc=l)
    assert r.status == "CONCILIADO" and "data do formulário corrigida" in r.motivo
    f.refresh_from_db()
    assert f.data_informada == date(2026, 7, 29)
    pagina = client.get(URL.format(c.rep.pk), REMOTE_ADDR=IP).content.decode()
    assert "09/07/2026</strong>" in pagina and "29/07/2026</s>" in pagina
    client.post(f"/resultado/{r.pk}/despesa/", {"campo": "DATA", "acao": "desfazer"}, REMOTE_ADDR=IP)
    assert Resultado.objects.get(vigente=True, fatura_lanc=l).status == "CONCILIADO_RESSALVA"


@pytest.mark.parametrize("valor", ["", "abc", "1999-01-01", "2026-13-01"])
def test_data_invalida_e_recusada(client, valor):
    c = Cenario()
    _, r = _form_sozinho(c)
    resp = client.post(f"/resultado/{r.pk}/despesa/", {"campo": "DATA", "valor": valor}, REMOTE_ADDR=IP, follow=True)
    assert "Data inválida" in resp.content.decode() and not CorrecaoDespesa.objects.exists()


def test_reembolso_em_todas_as_listas(client):
    c = Cenario()
    l = c.lanc(date(2026, 7, 9), "48.00", "RESTAURANTE")
    c.rodar()
    r = Resultado.objects.get(vigente=True, fatura_lanc=l)
    pagina = client.get(URL.format(c.rep.pk), REMOTE_ADDR=IP).content.decode()
    assert 'value="REEMBOLSO"' in pagina
    client.post(f"/resultado/{r.pk}/situacao/", {"status": "REEMBOLSO"}, REMOTE_ADDR=IP)
    assert Resultado.objects.get(vigente=True, fatura_lanc=l).status == "REEMBOLSO"


def test_linha_sem_formulario_tem_lista_com_nao_informado(client):
    from conciliacao.models import chave_lancamento
    c = Cenario()
    l = c.lanc(date(2026, 7, 9), "48.00", "POSTO SHELL")
    c.rodar()
    r = Resultado.objects.get(vigente=True, fatura_lanc=l)
    pagina = client.get(URL.format(c.rep.pk), REMOTE_ADDR=IP).content.decode()
    assert '<option value="" selected>Não informado</option>' in pagina and "sem formulário" in pagina
    client.post(f"/resultado/{r.pk}/despesa/", {"campo": "CATEGORIA", "valor": "Combustível"}, REMOTE_ADDR=IP)
    corr = CorrecaoDespesa.objects.get(ativo=True)
    assert (corr.chave, corr.original, corr.novo) == (chave_lancamento(l), "", "Combustível")
    pagina = client.get(URL.format(c.rep.pk), REMOTE_ADDR=IP).content.decode()
    assert '<option value="Combustível" selected>' in pagina and "categoria informada" in pagina
    assert Resultado.objects.get(vigente=True, fatura_lanc=l).status == "SEM_FORMULARIO"   # não muda a situação
    r = Resultado.objects.get(vigente=True, fatura_lanc=l)
    client.post(f"/resultado/{r.pk}/despesa/", {"campo": "CATEGORIA", "valor": "", "acao": "desfazer"}, REMOTE_ADDR=IP)
    assert not CorrecaoDespesa.objects.filter(ativo=True).exists()
    # sem formulário não há data para corrigir
    r = Resultado.objects.get(vigente=True, fatura_lanc=l)
    assert client.post(f"/resultado/{r.pk}/despesa/", {"campo": "DATA", "valor": "2026-07-01"},
                       REMOTE_ADDR=IP).status_code == 403
