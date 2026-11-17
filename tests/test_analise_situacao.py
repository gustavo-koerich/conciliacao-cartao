"""Regra: a situação que o financeiro define à mão vale no dashboard (reembolso x cartão)."""
from datetime import date

import pytest

from conciliacao import analise
from conciliacao.models import Resultado
from tests.test_motor import Cenario

pytestmark = pytest.mark.django_db
IP = "10.0.0.8"


def _pag(ref_pk):
    linha = next(l for l in analise.coletar()["linhas"] if l["ref"] == f"F{ref_pk}")
    return analise._pagamento(linha["pagamento"]), linha["valor"]


def test_fatura_conferida_a_mao_sem_formulario_entra_no_dashboard(client):
    from conciliacao import motor
    c = Cenario()
    l1 = c.lanc(date(2026, 7, 9), "48.00", "RESTAURANTE")
    l2 = c.lanc(date(2026, 7, 12), "35.00", "UBER")
    c.rodar()
    for lanc, cat in ((l1, "Alimentação pessoal"), (l2, "")):
        r = Resultado.objects.get(vigente=True, fatura_lanc=lanc)
        if cat:
            client.post(f"/resultado/{r.pk}/despesa/", {"campo": "CATEGORIA", "valor": cat, "motivo": "conferido"},
                        REMOTE_ADDR=IP)
            r = Resultado.objects.get(vigente=True, fatura_lanc=lanc)
        client.post(f"/resultado/{r.pk}/situacao/", {"status": "CONCILIADO_RESSALVA", "observacao": "conferido"},
                    REMOTE_ADDR=IP)
    linhas = {l["ref"]: l for l in analise.coletar()["linhas"] if l["fonte"] == "Fatura"}
    assert set(linhas) >= {f"L{l1.pk}", f"L{l2.pk}"}
    assert (linhas[f"L{l1.pk}"]["despesa"], linhas[f"L{l1.pk}"]["valor"]) == ("Alimentação pessoal", l1.valor)
    assert linhas[f"L{l1.pk}"]["origem"] == "Escolha do financeiro" and linhas[f"L{l1.pk}"]["conta_total"]
    assert linhas[f"L{l2.pk}"]["despesa"] == "Não informado" and linhas[f"L{l2.pk}"]["data"] == l2.data
    # a planilha chega e o gasto casa com o formulário: uma linha só, ainda com o valor da fatura
    f = c.form(date(2026, 7, 9), "48.00")
    motor.executar(c.comp)
    linhas = [l for l in analise.coletar()["linhas"] if l["valor"] == l1.valor]
    assert len(linhas) == 1 and linhas[0]["fonte"] == "Fatura" and linhas[0]["valor"] == l1.valor


def test_ajuste_manual_decide_reembolso_no_dashboard(client):
    c = Cenario()
    reemb = c.form(date(2026, 7, 1), "42.00", pagamento="Reembolso")
    cartao = c.form(date(2026, 7, 3), "30.00")
    c.lanc(date(2026, 7, 9), "99.00", "OUTRA LOJA")            # o motor precisa de uma fatura carregada
    c.rodar()
    assert _pag(reemb.pk) == ("Reembolso", reemb.valor)       # sem ajuste: vale o formulário
    assert _pag(cartao.pk)[0] == "Cartão"

    r = Resultado.objects.get(vigente=True, form_despesa=reemb)
    client.post(f"/resultado/{r.pk}/situacao/", {"status": "SEM_COBRANCA"}, REMOTE_ADDR=IP)
    assert Resultado.objects.get(vigente=True, form_despesa=reemb).status == "SEM_COBRANCA"
    assert _pag(reemb.pk) == ("Cartão", reemb.valor)          # financeiro tirou de reembolso: continua no total

    r = Resultado.objects.get(vigente=True, form_despesa=cartao)
    client.post(f"/resultado/{r.pk}/situacao/", {"status": "REEMBOLSO"}, REMOTE_ADDR=IP)
    assert _pag(cartao.pk)[0] == "Reembolso"                  # financeiro marcou reembolso
