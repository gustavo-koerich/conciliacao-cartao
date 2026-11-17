"""Restrições do banco que garantem a regra de negócio mesmo se o código errar."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from conciliacao.models import (Cartao, Competencia, ContaCartao, Execucao, FaturaLanc, FormDespesa, Importacao, Representante,
                                Resultado)

pytestmark = pytest.mark.django_db


@pytest.fixture
def competencia():
    return Competencia.objects.create(ano_mes="2026-08", periodo_ini=date(2026, 7, 1), periodo_fim=date(2026, 8, 1),
                                      fechamento=date(2026, 8, 1), vencimento=date(2026, 8, 11),
                                      total_fatura=Decimal("252432.75"))


@pytest.fixture
def fabio():
    rep = Representante.objects.create(nome="Fábio")
    Cartao.objects.create(final="5501", nome_fatura="FABIO LUIZ N SALES", representante=rep)
    return rep


def _fatura(competencia, versao, ativo=False, status="OK"):
    return Importacao.objects.create(tipo="FATURA", competencia=competencia, versao=versao, origem="UPLOAD",
                                     ativo=ativo, status=status)


def _erro_integridade(func):
    with pytest.raises(IntegrityError), transaction.atomic():
        func()


def test_todas_as_tabelas_tem_prefixo_conc():
    nomes = [m._meta.db_table for m in apps.get_app_config("conciliacao").get_models()]
    assert nomes and all(n.startswith("conc_") for n in nomes)


def test_so_uma_versao_ativa_de_fatura_por_competencia(competencia):
    _fatura(competencia, 1, ativo=True)
    _fatura(competencia, 2, ativo=False)
    _erro_integridade(lambda: _fatura(competencia, 3, ativo=True))


def test_so_uma_planilha_ativa_por_representante_com_competencia_nula(fabio):
    Importacao.objects.create(tipo="PLANILHA", representante=fabio, versao=1, origem="GOOGLE_SHEETS",
                              ativo=True, status="OK")
    _erro_integridade(lambda: Importacao.objects.create(tipo="PLANILHA", representante=fabio, versao=2,
                                                        origem="UPLOAD", ativo=True, status="OK"))


def test_numero_de_versao_nao_se_repete(competencia):
    _fatura(competencia, 1)
    _erro_integridade(lambda: _fatura(competencia, 1))


def test_fatura_ok_exige_competencia_e_planilha_exige_representante():
    # Processando ou com erro de leitura, a fatura ainda pode não ter competência.
    Importacao.objects.create(tipo="FATURA", origem="UPLOAD", status="PROCESSANDO")
    Importacao.objects.create(tipo="FATURA", origem="UPLOAD", status="ERRO")
    _erro_integridade(lambda: Importacao.objects.create(tipo="FATURA", versao=1, origem="UPLOAD", status="OK"))
    _erro_integridade(lambda: Importacao.objects.create(tipo="PLANILHA", versao=1, origem="UPLOAD"))


def test_importacao_ok_exige_versao(competencia):
    _erro_integridade(lambda: Importacao.objects.create(tipo="FATURA", competencia=competencia, origem="UPLOAD",
                                                        status="OK"))


def test_importacao_com_erro_nao_pode_ficar_ativa(competencia):
    _erro_integridade(lambda: _fatura(competencia, 1, ativo=True, status="ERRO"))


def test_lancamentos_identicos_legitimos_com_ocorrencia_diferente(competencia, fabio):
    imp = _fatura(competencia, 1)
    cartao = fabio.cartoes.get()
    base = dict(importacao=imp, cartao=cartao, secao="PORTADOR", natureza="COMPRA", data=date(2026, 7, 2),
                descricao="ESTAPAR", valor=Decimal("10.00"))
    FaturaLanc.objects.create(ordem=1, n_ocorrencia=1, **base)
    FaturaLanc.objects.create(ordem=2, n_ocorrencia=2, **base)
    _erro_integridade(lambda: FaturaLanc.objects.create(ordem=3, n_ocorrencia=2, **base))


def test_parcela_atual_nao_passa_do_total(competencia, fabio):
    imp = _fatura(competencia, 1)
    _erro_integridade(lambda: FaturaLanc.objects.create(
        importacao=imp, cartao=fabio.cartoes.get(), secao="PORTADOR", natureza="COMPRA", data=date(2026, 7, 2),
        descricao="LOJA", valor=Decimal("10.00"), parcela_atual=6, parcela_total=5, ordem=1))


def test_lancamento_de_portador_exige_cartao(competencia):
    imp = _fatura(competencia, 1)
    _erro_integridade(lambda: FaturaLanc.objects.create(
        importacao=imp, secao="PORTADOR", natureza="COMPRA", data=date(2026, 7, 2), descricao="X",
        valor=Decimal("1.00"), ordem=1))
    FaturaLanc.objects.create(importacao=imp, secao="CONTA", natureza="ENCARGO", data=date(2026, 7, 2),
                              descricao="IOF OPERACAO EXTERIOR", valor=Decimal("51.27"), ordem=2)


def test_carimbo_unico_por_versao_da_planilha(fabio):
    imp = Importacao.objects.create(tipo="PLANILHA", representante=fabio, versao=1, origem="UPLOAD")
    carimbo = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
    FormDespesa.objects.create(importacao=imp, representante=fabio, linha_planilha=2, carimbo=carimbo)
    _erro_integridade(lambda: FormDespesa.objects.create(importacao=imp, representante=fabio, linha_planilha=3,
                                                         carimbo=carimbo))


def test_resultado_precisa_de_pelo_menos_um_lado(competencia):
    exe = Execucao.objects.create(competencia=competencia, parametros={})
    _erro_integridade(lambda: Resultado.objects.create(execucao=exe, competencia=competencia, status="CONCILIADO"))


def test_final_do_cartao_tem_quatro_digitos():
    with pytest.raises(ValidationError):
        Cartao(final="84A9", nome_fatura="X").full_clean()


def test_cartao_e_unico_pelo_final_dentro_da_conta(fabio):
    _erro_integridade(lambda: Cartao.objects.create(final="5501", nome_fatura="OUTRO NOME"))
    # Outro banco pode ter um cartão com o mesmo final.
    bb = ContaCartao.objects.create(nome="BB Ourocard", banco="BB", slug="bb", leitor="BB")
    Cartao.objects.create(conta=bb, final="5501", nome_fatura="OUTRO PORTADOR")


def test_competencia_e_unica_por_conta_e_mes(competencia):
    bb = ContaCartao.objects.create(nome="BB Ourocard", banco="BB", slug="bb", leitor="BB")
    outra = Competencia.objects.create(conta=bb, ano_mes="2026-08", periodo_ini=date(2026, 7, 5),
                                       periodo_fim=date(2026, 8, 4), fechamento=date(2026, 8, 4),
                                       vencimento=date(2026, 8, 17), total_fatura=Decimal("1"))
    assert outra.codigo == "bb-2026-08" and competencia.codigo == "ailos-2026-08"
    assert Competencia.por_codigo("bb-2026-08") == outra
    _erro_integridade(lambda: Competencia.objects.create(
        conta=bb, ano_mes="2026-08", periodo_ini=date(2026, 7, 5), periodo_fim=date(2026, 8, 4),
        fechamento=date(2026, 8, 4), vencimento=date(2026, 8, 17), total_fatura=Decimal("1")))


def test_competencia_formato_ano_mes():
    with pytest.raises(ValidationError):
        Competencia(ano_mes="08/2026", periodo_ini=date(2026, 7, 1), periodo_fim=date(2026, 8, 1),
                    fechamento=date(2026, 8, 1), vencimento=date(2026, 8, 11), total_fatura=1).full_clean()
