"""Conciliação entre meses: formulário nunca vale em dois meses, sem pendência falsa, parcelas seguintes."""
from datetime import date
from decimal import Decimal as D

import pytest

from conciliacao import motor
from conciliacao.models import Competencia, FaturaLanc, Importacao, Resultado
from tests.test_motor import Cenario, c  # noqa: F401

pytestmark = pytest.mark.django_db


def _mes_seguinte(c, ano_mes="2026-09", ini=date(2026, 8, 3), fech=date(2026, 9, 1), com_planilha=True):
    comp = Competencia.objects.create(ano_mes=ano_mes, periodo_ini=ini, periodo_fim=fech, fechamento=fech,
                                      vencimento=fech, total_fatura=D("0"))
    fatura = Importacao.objects.create(tipo="FATURA", competencia=comp, versao=1, origem="UPLOAD", status="OK",
                                       ativo=True)
    if com_planilha:
        Importacao.objects.create(tipo="PLANILHA", representante=c.rep, competencia=comp, versao=1, origem="UPLOAD",
                                  status="OK", ativo=True)
    return comp, fatura


def _lanc(c, fatura, comp, dia, valor, desc="POSTO", parcela=None):
    l = FaturaLanc.objects.create(importacao=fatura, cartao=c.cartao, secao="PORTADOR", natureza="COMPRA", data=dia,
                                  descricao=desc, valor=D(valor), ordem=FaturaLanc.objects.count() + 1,
                                  parcela_atual=parcela[0] if parcela else None,
                                  parcela_total=parcela[1] if parcela else None)
    Competencia.objects.filter(pk=comp.pk).update(total_fatura=comp.total_fatura + D(valor))
    comp.refresh_from_db()
    return l


def _vig(**kw):
    return Resultado.objects.get(vigente=True, **kw)


def test_formulario_casado_na_fatura_seguinte_nao_fica_pendente_no_mes_dele(c):
    """Gasto de 31/07 lançado na planilha de julho só aparece na fatura de setembro: casa lá e, em julho, aparece
    como "conciliado na fatura …" (antes ficava "Sem cobrança"/"Pendente de análise")."""
    set_, fat = _mes_seguinte(c)
    f = c.form(date(2026, 7, 31), "88.00", "Combustível")
    c.lanc(date(2026, 7, 9), "10.00")
    c.rodar()
    assert _vig(form_despesa=f, competencia=c.comp).status == "SEM_COBRANCA"
    l = _lanc(c, fat, set_, date(2026, 7, 31), "88.00")
    motor.executar(set_)                       # a cascata recalcula julho sozinha
    assert _vig(fatura_lanc=l).status == "CONCILIADO"
    r = _vig(form_despesa=f, competencia=c.comp)
    assert r.status == "CONCILIADO" and "conciliado na fatura" in r.motivo


def test_formulario_nao_casa_em_dois_meses(c):
    set_, fat = _mes_seguinte(c)
    f = c.form(date(2026, 7, 31), "88.00")
    l_jul = c.lanc(date(2026, 7, 31), "88.00")
    l_set = _lanc(c, fat, set_, date(2026, 8, 2), "88.00")       # mesmo valor, dentro da janela de datas
    motor.executar(set_)                        # setembro primeiro: pega o formulário
    c.rodar()                                   # julho tem prioridade: fica com ele e setembro é recalculado
    assert _vig(fatura_lanc=l_jul).form_despesa_id == f.pk
    assert _vig(fatura_lanc=l_set).status == "SEM_FORMULARIO"
    assert Resultado.objects.filter(vigente=True, form_despesa=f, fatura_lanc__isnull=False).count() == 1


def test_parcela_seguinte_de_compra_conciliada_fica_conciliada(c):
    """10x de 100 começando em julho e conciliada em julho: a parcela 2/10 de setembro concilia sozinha."""
    set_, fat = _mes_seguinte(c, com_planilha=False)            # nem precisa da planilha do mês
    c.form(date(2026, 7, 5), "1000.00", "Manutenção veicular")
    l1 = c.lanc(date(2026, 7, 5), "100.00", "PNEUS", parcela=(1, 10))
    c.rodar()
    assert _vig(fatura_lanc=l1).status in ("CONCILIADO", "CONCILIADO_RESSALVA")
    l2 = _lanc(c, fat, set_, date(2026, 7, 5), "100.00", "PNEUS", parcela=(2, 10))
    outra = _lanc(c, fat, set_, date(2026, 8, 10), "55.00", "LOJA")
    motor.executar(set_)
    r = _vig(fatura_lanc=l2)
    assert (r.status, r.regra) == ("CONCILIADO", "PARCELADO") and "parcela 2/10" in r.motivo
    assert _vig(fatura_lanc=outra).status == "AGUARDANDO_PLANILHA"


def test_parcela_sem_compra_conciliada_continua_pendente(c):
    set_, fat = _mes_seguinte(c)
    c.lanc(date(2026, 7, 9), "10.00")
    c.rodar()
    l2 = _lanc(c, fat, set_, date(2026, 7, 5), "100.00", "PNEUS", parcela=(2, 10))
    motor.executar(set_)
    assert _vig(fatura_lanc=l2).status == "SEM_FORMULARIO"


def test_nao_deixa_corrigir_valor_de_gasto_que_ja_fecha_conta_dividida(c, client):
    """Caso real: formulário de conta dividida (32 + 351,60 = 383,60) corrigido para 383,60 passaria a valer duas
    vezes. A correção é recusada com a explicação do que fazer."""
    from conciliacao.models import CorrecaoValor
    a = c.form(date(2026, 7, 5), "32.00", "Combustível")
    b = c.form(date(2026, 7, 5), "351.60", "Manutenção veicular")
    l = c.lanc(date(2026, 7, 5), "383.60", "PNEUS")
    c.rodar()
    assert _vig(form_despesa=b).regra == "DIVIDIDA"
    r = _vig(form_despesa=b)
    resp = client.post(f"/resultado/{r.pk}/valor/", {"valor": "383,60", "motivo": "conferido"},
                       REMOTE_ADDR="10.0.0.8", follow=True)
    assert "já está conciliado na conta dividida" in resp.content.decode()
    assert not CorrecaoValor.objects.exists() and _vig(form_despesa=a).fatura_lanc_id == l.pk
