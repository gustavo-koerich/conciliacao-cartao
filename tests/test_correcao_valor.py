"""Correção do valor informado pelo representante (o da fatura não se corrige) e ressalva com motivo obrigatório."""
from datetime import date
from decimal import Decimal

import pytest

from conciliacao.models import AjusteManual, Auditoria, CorrecaoValor, Resultado
from tests.test_motor import Cenario

pytestmark = pytest.mark.django_db
IP = "10.0.0.8"
URL = "/painel/ailos-2026-08/representante/{}/"


def _valor_errado():
    """Fatura R$ 48,00; o representante informou R$ 84,00 (dígitos trocados): não conciliam."""
    c = Cenario()
    l = c.lanc(date(2026, 7, 9), "48.00", "RESTAURANTE")
    f = c.form(date(2026, 7, 9), "84.00")
    c.rodar()
    return c, l, f


def test_corrigir_o_valor_informado_concilia_e_guarda_o_original(client):
    c, l, f = _valor_errado()
    assert Resultado.objects.get(vigente=True, fatura_lanc=l).status == "SEM_FORMULARIO"
    r_form = Resultado.objects.get(vigente=True, form_despesa=f, fatura_lanc__isnull=True)
    resp = client.post(f"/resultado/{r_form.pk}/valor/", {"valor": "48,00", "motivo": "conferido no recibo",
                                                          "voltar": URL.format(c.rep.pk)}, REMOTE_ADDR=IP)
    assert resp.status_code == 302 and f"linha=F{f.pk}" in resp["Location"]
    r = Resultado.objects.get(vigente=True, fatura_lanc=l)
    assert r.status == "CONCILIADO" and r.form_despesa_id == f.pk          # correção não vira ressalva sozinha
    assert "corrigido de" in r.motivo and "conferido no recibo" in r.motivo
    f.refresh_from_db()
    assert f.valor == Decimal("84.00")                                      # o original não é apagado
    corr = CorrecaoValor.objects.get(ativo=True)
    assert (corr.valor_original, corr.valor_corrigido) == (Decimal("84.00"), Decimal("48.00"))
    assert Auditoria.objects.filter(acao="AJUSTE_MANUAL", detalhe__correcao_valor=True).exists()
    pagina = client.get(URL.format(c.rep.pk), REMOTE_ADDR=IP).content.decode()
    assert "valor corrigido" in pagina and "<s " in pagina and "voltar ao informado" in pagina


def test_desfazer_volta_ao_valor_informado(client):
    c, l, f = _valor_errado()
    r_form = Resultado.objects.get(vigente=True, form_despesa=f, fatura_lanc__isnull=True)
    client.post(f"/resultado/{r_form.pk}/valor/", {"valor": "48", "motivo": "recibo"}, REMOTE_ADDR=IP)
    r = Resultado.objects.get(vigente=True, fatura_lanc=l)
    client.post(f"/resultado/{r.pk}/valor/", {"acao": "desfazer"}, REMOTE_ADDR=IP)
    assert Resultado.objects.get(vigente=True, fatura_lanc=l).status == "SEM_FORMULARIO"
    assert not CorrecaoValor.objects.filter(ativo=True).exists() and CorrecaoValor.objects.count() == 1


def test_valor_ilegivel_corrigido_passa_a_conciliar(client):
    c = Cenario()
    l = c.lanc(date(2026, 7, 9), "130.00", "POSTO")
    f = c.form(date(2026, 7, 9), None, despesa="Combustível", valor_invalido=True, valor_original="130 po")
    c.rodar()
    r_form = Resultado.objects.get(vigente=True, form_despesa=f)
    assert r_form.status == "PENDENTE_ANALISE"
    client.post(f"/resultado/{r_form.pk}/valor/", {"valor": "R$ 130,00", "motivo": "digitou errado"}, REMOTE_ADDR=IP)
    assert Resultado.objects.get(vigente=True, fatura_lanc=l).status == "CONCILIADO"


@pytest.mark.parametrize("dados,erro", [({"valor": "abc", "motivo": "x y z"}, "Valor inválido"),
                                        ({"valor": "0", "motivo": "x y z"}, "Valor inválido"),
                                        ({"valor": "48,00", "motivo": ""}, "motivo")])
def test_valor_ou_motivo_invalido_e_recusado(client, dados, erro):
    c, l, f = _valor_errado()
    r_form = Resultado.objects.get(vigente=True, form_despesa=f, fatura_lanc__isnull=True)
    resp = client.post(f"/resultado/{r_form.pk}/valor/", dados, REMOTE_ADDR=IP, follow=True)
    assert erro in resp.content.decode() and not CorrecaoValor.objects.exists()


def test_valor_da_fatura_nao_se_corrige(client):
    c, l, f = _valor_errado()
    r_fatura = Resultado.objects.get(vigente=True, fatura_lanc=l)
    assert client.post(f"/resultado/{r_fatura.pk}/valor/", {"valor": "10", "motivo": "teste"},
                       REMOTE_ADDR=IP).status_code == 403


def test_ressalva_manual_exige_motivo_e_fica_amarela(client):
    c, l, f = _valor_errado()
    r_form = Resultado.objects.get(vigente=True, form_despesa=f, fatura_lanc__isnull=True)
    pagina = client.get(URL.format(c.rep.pk), REMOTE_ADDR=IP).content.decode()
    assert 'value="CONCILIADO_RESSALVA" data-cor="linha-amarela" data-motivo="1"' in pagina   # agora também no formulário
    assert 'value="CONCILIADO" data-cor="linha-verde"' in pagina
    resp = client.post(f"/resultado/{r_form.pk}/situacao/", {"status": "CONCILIADO_RESSALVA"}, REMOTE_ADDR=IP, follow=True)
    assert "escreva o motivo" in resp.content.decode() and not AjusteManual.objects.exists()
    client.post(f"/resultado/{r_form.pk}/situacao/", {"status": "CONCILIADO_RESSALVA", "observacao": "recibo ilegível"},
                REMOTE_ADDR=IP)
    r = Resultado.objects.get(vigente=True, form_despesa=f, fatura_lanc__isnull=True)
    assert r.status == "CONCILIADO_RESSALVA"
    assert 'class="linha-amarela' in client.get(URL.format(c.rep.pk), REMOTE_ADDR=IP).content.decode()
