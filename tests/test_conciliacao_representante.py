"""Critério de aceite da seção 12: Fábio (5501), competência agosto/2026, com os arquivos reais."""
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from django.db.models import Count, Sum

from conciliacao import parametros
from conciliacao.fatura import servico as fatura_servico
from conciliacao.planilha.normalizacao import normalizar
from conciliacao.models import Cartao, FormDespesa, Importacao, Representante, Resultado
from conciliacao.planilha import servico as planilha_servico

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).parent / "fixtures"
AGOSTO = FIXTURES / "fatura-2026-08.pdf"
PLANILHA = FIXTURES / "planilha-fabio-5501.xlsx"


def _status_fabio(comp):
    qs = Resultado.objects.filter(competencia=comp, vigente=True, representante__nome="Fábio")
    return {x["status"]: (x["n"], x["v"]) for x in qs.values("status").annotate(n=Count("id"), v=Sum("valor_trava"))}


def test_fabio_agosto_2026(tmp_path):
    if not (AGOSTO.exists() and PLANILHA.exists()):
        pytest.skip("fixtures ausentes (ficam fora do git)")
    fabio = Representante.objects.create(nome="Fábio")
    Cartao.objects.create(final="5501", nome_fatura="FABIO LUIZ N SALES", representante=fabio)
    Cartao.objects.create(final="5502", nome_fatura="RODRIGO R DE S JUNIOR",
                          representante=Representante.objects.create(nome="Rodrigo"))

    imp, _ = fatura_servico.criar_importacao(AGOSTO, AGOSTO.name, usuario="teste", ip="10.0.0.5")
    fatura = fatura_servico.processar(imp.pk)
    assert fatura.ativo
    comp = fatura.competencia

    p, _ = planilha_servico.criar_importacao(PLANILHA, PLANILHA.name, fabio, comp, usuario="teste", ip="10.0.0.5")
    p = planilha_servico.processar(p.pk)
    assert p.status == "OK" and p.ativo
    # Planilha completa enviada para agosto: só ficam as respostas do período (01/07 a 03/08).
    assert p.resumo["respostas_na_planilha"] == 407 and p.resumo["outros_periodos"] > 0
    assert not FormDespesa.objects.filter(importacao=p).exclude(
        data_corrigida__range=(comp.periodo_ini, comp.fechamento)).exclude(data_corrigida=None).exists()

    st = _status_fabio(comp)
    conciliado_n = st["CONCILIADO"][0] + st["CONCILIADO_RESSALVA"][0]
    conciliado_v = st["CONCILIADO"][1] + st["CONCILIADO_RESSALVA"][1]
    assert (conciliado_n, conciliado_v) == (15, Decimal("4355.38"))
    assert st["CONCILIADO_RESSALVA"][0] == 2
    assert st["SEM_FORMULARIO"] == (45, Decimal("6073.00"))
    assert st["ESTORNO_COMPENSADO"] == (2, Decimal("0.00"))
    assert st["REEMBOLSO"][0] == 1

    vig = Resultado.objects.filter(competencia=comp, vigente=True, representante__nome="Fábio")
    tivoli = vig.get(fatura_lanc__valor=Decimal("82.70"))
    assert tivoli.status == "CONCILIADO_RESSALVA" and "03/07/2002 para 03/07/2026" in tivoli.motivo
    tucuruvi = vig.get(fatura_lanc__descricao__startswith="TUCURUVI")
    assert tucuruvi.status == "CONCILIADO_RESSALVA" and "Alimentação" in tucuruvi.motivo
    sem_form = vig.filter(status="SEM_FORMULARIO")
    assert sem_form.filter(fatura_lanc__valor=Decimal("20.00"), fatura_lanc__descricao__icontains="Jucimar").exists()
    assert sem_form.filter(fatura_lanc__valor=Decimal("68.84"), fatura_lanc__descricao__icontains="TUPINAMBA").exists()
    depois_de_6 = vig.filter(fatura_lanc__data__gte="2026-07-06", fatura_lanc__data__lte="2026-07-31")
    assert depois_de_6.exists() and not depois_de_6.exclude(status="SEM_FORMULARIO").exists()
    reembolso = vig.get(status="REEMBOLSO")
    assert (reembolso.form_despesa.valor, str(reembolso.form_despesa.data_efetiva)) == (Decimal("25.00"), "2026-07-01")
    assert Resultado.objects.filter(execucao__competencia=comp, vigente=True).exists()
    assert comp.execucoes.order_by("-inicio").first().status == "OK"   # trava fecha ao centavo

    # Duplicidades de 15/09/2025 marcadas (período sem fatura no sistema: conferido na normalização do arquivo)
    normalizada = normalizar(PLANILHA, parametros.obter_todos())
    assert sum(1 for d in normalizada.despesas if str(d.data_corrigida) == "2025-09-15" and d.duplicado) == 4

    # Atualizar a planilha do Fábio reconcilia só o cartão 5501
    outros = set(Resultado.objects.filter(competencia=comp, vigente=True).exclude(cartao__final="5501")
                 .values_list("id", flat=True))
    nova = tmp_path / "fabio-v2.xlsx"
    pd.read_excel(PLANILHA, sheet_name=None)["Respostas ao formulário 1"].to_excel(nova, index=False,
                                                                                    sheet_name="Respostas")
    p2, criada = planilha_servico.criar_importacao(nova, nova.name, fabio, comp, usuario="teste")
    assert criada
    p2 = planilha_servico.processar(p2.pk)
    assert p2.ativo and not Importacao.objects.get(pk=p.pk).ativo
    assert set(Resultado.objects.filter(competencia=comp, vigente=True).exclude(cartao__final="5501")
               .values_list("id", flat=True)) == outros
    assert _status_fabio(comp)["SEM_FORMULARIO"] == (45, Decimal("6073.00"))

    # Reimportar o mesmo arquivo não cria nada
    _, criada = planilha_servico.criar_importacao(nova, nova.name, fabio, comp)
    assert not criada
