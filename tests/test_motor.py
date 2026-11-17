"""Regras do motor de conciliação, com dados montados no teste."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from conciliacao import motor
from conciliacao.models import (AjusteManual, Anexo, Cartao, Competencia, Execucao, FaturaLanc, FormDespesa,
                                Importacao, Representante, Resultado, chave_despesa, chave_lancamento,
                                chave_resultado)

pytestmark = pytest.mark.django_db
FUSO = ZoneInfo("America/Sao_Paulo")
D = Decimal


class Cenario:
    """Uma competência de julho (01/07 a 03/08) com o cartão 5501 do Fábio e o 5507 (fase 2)."""

    def __init__(self):
        self.rep = Representante.objects.create(nome="Fábio")
        self.cartao = Cartao.objects.create(final="5501", nome_fatura="FABIO", representante=self.rep)
        self.fase2 = Cartao.objects.create(final="5507", nome_fatura="JOAO", escopo="FASE2_NF")
        self.comp = Competencia.objects.create(ano_mes="2026-08", periodo_ini=date(2026, 7, 1),
                                               periodo_fim=date(2026, 8, 1), fechamento=date(2026, 8, 3),
                                               vencimento=date(2026, 8, 11), total_fatura=D("0"))
        self.fatura = Importacao.objects.create(tipo="FATURA", competencia=self.comp, versao=1, origem="UPLOAD",
                                                status="OK", ativo=True)
        self.planilha = Importacao.objects.create(tipo="PLANILHA", representante=self.rep, competencia=self.comp,
                                                  versao=1, origem="UPLOAD", status="OK", ativo=True)
        self.ordem = 0
        self.minuto = 0

    def lanc(self, dia, valor, desc="LOJA", cartao=None, parcela=None, moeda="BRL", secao="PORTADOR",
             natureza=None):
        self.ordem += 1
        valor = D(valor)
        if natureza is None:
            natureza = "ENCARGO" if secao == "CONTA" else ("CREDITO" if valor < 0 else "COMPRA")
        lanc = FaturaLanc.objects.create(
            importacao=self.fatura, cartao=None if secao == "CONTA" else (cartao or self.cartao), secao=secao,
            natureza=natureza, data=dia, descricao=desc, valor=valor, ordem=self.ordem,
            n_ocorrencia=self.ordem, moeda=moeda,
            parcela_atual=parcela[0] if parcela else None, parcela_total=parcela[1] if parcela else None)
        self.comp.total_fatura += valor
        self.comp.save()
        return lanc

    def form(self, dia, valor, despesa="Alimentação", pagamento="Cartão Crédito", link="https://drive/x", **extra):
        self.minuto += 1
        carimbo = datetime(2026, 7, 25, 9, 0, tzinfo=FUSO) + timedelta(minutes=self.minuto)
        return FormDespesa.objects.create(
            importacao=self.planilha, representante=self.rep, linha_planilha=self.minuto + 1, carimbo=carimbo,
            data_informada=dia, data_corrigida=extra.pop("data_corrigida", dia), despesa=despesa,
            pagamento=pagamento, valor=D(valor) if valor is not None else None, link_comprovante=link, **extra)

    def rodar(self):
        execucao = motor.executar(self.comp)
        return execucao, Resultado.objects.filter(execucao=execucao)


@pytest.fixture
def c():
    return Cenario()


def _status(qs, lanc=None, form=None):
    if lanc is not None:
        return qs.get(fatura_lanc=lanc).status
    return qs.get(form_despesa=form, fatura_lanc=None).status


def test_valor_exato_e_janela_de_datas(c):
    l1 = c.lanc(date(2026, 7, 1), "15.00", "SKIPARK ESTACIONAMEN")
    l2 = c.lanc(date(2026, 7, 2), "40.00", "SKIPARK ESTACIONAMEN")
    f1 = c.form(date(2026, 7, 1), "15.00", "Estacionamento")
    c.form(date(2026, 7, 4), "40.00", "Estacionamento")
    exe, r = c.rodar()
    assert exe.status == "OK"
    a, b = r.get(fatura_lanc=l1), r.get(fatura_lanc=l2)
    assert (a.status, a.regra, a.form_despesa_id) == ("CONCILIADO", "EXATO", f1.pk)
    assert (b.status, b.regra) == ("CONCILIADO", "JANELA")


def test_desempate_pela_menor_distancia(c):
    l = c.lanc(date(2026, 7, 10), "10.00", "ESTAPAR")
    c.form(date(2026, 7, 7), "10.00", "Estacionamento")
    perto = c.form(date(2026, 7, 9), "10.00", "Estacionamento")
    _, r = c.rodar()
    assert r.get(fatura_lanc=l).form_despesa_id == perto.pk


def test_data_distante_vira_ressalva_so_dentro_da_competencia(c):
    l1 = c.lanc(date(2026, 7, 1), "99.00", "RESTAURANTE")
    l2 = c.lanc(date(2026, 7, 2), "77.00", "RESTAURANTE")
    c.form(date(2026, 7, 20), "99.00")          # mesma competência, 19 dias depois
    c.form(date(2026, 8, 20), "77.00")          # fora do período: não casa
    _, r = c.rodar()
    a = r.get(fatura_lanc=l1)
    assert a.status == "CONCILIADO_RESSALVA" and "19 dias" in a.motivo
    assert _status(r, lanc=l2) == "SEM_FORMULARIO"


def test_estorno_vem_antes_do_casamento(c):
    p = c.lanc(date(2026, 7, 3), "150.75", "PG *TUPINAMBA")
    n = c.lanc(date(2026, 7, 3), "-150.75", "PG *TUPINAMBA")
    f = c.form(date(2026, 7, 3), "150.75")
    _, r = c.rodar()
    assert _status(r, lanc=p) == _status(r, lanc=n) == "ESTORNO_COMPENSADO"
    assert r.get(fatura_lanc=p).grupo == r.get(fatura_lanc=n).grupo
    assert _status(r, form=f) == "SEM_COBRANCA"


def test_credito_sem_compra_vai_para_analise(c):
    n = c.lanc(date(2026, 7, 7), "-1090.62", "Tkt Aereo")
    _, r = c.rodar()
    assert _status(r, lanc=n) == "PENDENTE_ANALISE"


def test_parcelado_casa_com_o_total_e_parcelas_seguintes_tambem(c):
    l1 = c.lanc(date(2026, 7, 15), "33.33", "LOJA X", parcela=(1, 3))
    l2 = c.lanc(date(2026, 5, 15), "50.00", "LOJA Y", parcela=(2, 2))
    c.form(date(2026, 7, 15), "100.00", "Outros")      # 33,33 × 3 = 99,99 (tolerância 0,05 × 3)
    c.form(date(2026, 5, 15), "100.00", "Outros")
    exe, r = c.rodar()
    assert r.get(fatura_lanc=l1).regra == r.get(fatura_lanc=l2).regra == "PARCELADO"
    assert exe.vinculos.filter(fatura_lanc=l2).exists() and not exe.vinculos.filter(fatura_lanc=l1).exists()


def test_conta_dividida_nos_dois_sentidos(c):
    l = c.lanc(date(2026, 7, 10), "120.00", "RESTAURANTE")
    c.form(date(2026, 7, 10), "70.00")
    c.form(date(2026, 7, 10), "50.00")
    a = c.lanc(date(2026, 7, 12), "30.00", "POSTO", )
    b = c.lanc(date(2026, 7, 12), "20.00", "POSTO")
    f = c.form(date(2026, 7, 12), "50.00", "Combustível")
    exe, r = c.rodar()
    linhas = r.filter(fatura_lanc=l)
    assert linhas.count() == 2 and {x.regra for x in linhas} == {"DIVIDIDA"}
    assert sum(x.valor_trava or 0 for x in linhas) == D("120.00")      # o lançamento conta uma vez na trava
    assert r.get(fatura_lanc=a).form_despesa_id == r.get(fatura_lanc=b).form_despesa_id == f.pk
    assert exe.status == "OK"


def test_ressalvas_categoria_e_sem_recibo(c):
    l1 = c.lanc(date(2026, 7, 1), "10.00", "TUCURUVI PARKING")
    l2 = c.lanc(date(2026, 7, 2), "55.00", "PADARIA")
    c.form(date(2026, 7, 1), "10.00", "Alimentação")
    c.form(date(2026, 7, 2), "55.00", "Alimentação", link="foto.jpg")
    _, r = c.rodar()
    a, b = r.get(fatura_lanc=l1), r.get(fatura_lanc=l2)
    assert a.status == "CONCILIADO_RESSALVA" and "Estacionamento" in a.motivo
    assert b.status == "CONCILIADO_RESSALVA" and b.sem_comprovante and "sem recibo" in b.motivo


def test_data_corrigida_e_ressalva(c):
    l = c.lanc(date(2026, 7, 3), "82.70", "CASA TIVOLI")
    c.form(date(2002, 7, 3), "82.70", data_corrigida=date(2026, 7, 3), data_suspeita=True)
    _, r = c.rodar()
    x = r.get(fatura_lanc=l)
    assert (x.status, x.regra) == ("CONCILIADO_RESSALVA", "DATA_CORRIGIDA") and "2002" in x.motivo


def test_formulario_fora_do_cartao(c):
    reemb = c.form(date(2026, 7, 1), "25.00", pagamento="Reembolso")
    dup = c.form(date(2026, 7, 2), "35.00", duplicado=True)
    outro = c.form(date(2026, 7, 2), "30.00", pagamento="Cartão Crédito João", pagamento_outro_cartao=True)
    ilegivel = c.form(date(2026, 7, 2), None, valor_invalido=True, valor_original="130 po")
    dolar = c.form(date(2026, 7, 2), None, valor_invalido=True, moeda_estrangeira=True)
    sem_data = c.form(None, "12.00", data_suspeita=True, data_corrigida=None)
    c.lanc(date(2026, 7, 2), "35.00")                      # o duplicado não pode casar com este
    _, r = c.rodar()
    assert _status(r, form=reemb) == "REEMBOLSO"
    assert _status(r, form=dup) == "DUPLICADO_FORM"
    assert _status(r, form=outro) == "FORA_ESCOPO"
    assert _status(r, form=ilegivel) == _status(r, form=dolar) == _status(r, form=sem_data) == "PENDENTE_ANALISE"
    assert r.filter(status="SEM_FORMULARIO").count() == 1


def test_moeda_estrangeira_fora_escopo_e_encargos(c):
    us = c.lanc(date(2026, 7, 14), "1408.86", "DNH-MTOP", moeda="USD")
    joao = c.lanc(date(2026, 7, 14), "384.00", "WIX", cartao=c.fase2)
    iof = c.lanc(date(2026, 7, 14), "49.31", "IOF OPERACAO EXTERIOR", secao="CONTA")
    exe, r = c.rodar()
    assert _status(r, lanc=us) == "PENDENTE_ANALISE"
    assert _status(r, lanc=joao) == "SEM_NOTA"          # cartão de notas fiscais: espera a NF
    assert _status(r, lanc=iof) == "ENCARGOS_CONTA"
    assert exe.status == "OK" and D(exe.resumo["diferenca"]) == 0


def test_sem_planilha_fica_aguardando(c):
    c.planilha.ativo = False
    c.planilha.save()
    l = c.lanc(date(2026, 7, 1), "15.00")
    _, r = c.rodar()
    assert _status(r, lanc=l) == "AGUARDANDO_PLANILHA"


def test_trava_reprova_quando_a_soma_nao_fecha(c):
    c.lanc(date(2026, 7, 1), "15.00")
    Competencia.objects.filter(pk=c.comp.pk).update(total_fatura=D("15.01"))
    c.comp.refresh_from_db()
    exe, _ = c.rodar()
    assert exe.status == "ERRO" and D(exe.resumo["diferenca"]) == D("-0.01")


def test_reprocessar_um_cartao_preserva_os_outros(c):
    c.lanc(date(2026, 7, 1), "15.00")
    c.lanc(date(2026, 7, 14), "384.00", cartao=c.fase2)
    c.rodar()
    antes = set(Resultado.objects.filter(vigente=True, cartao=c.fase2).values_list("id", flat=True))
    exe = motor.executar(c.comp, [c.cartao])
    assert set(Resultado.objects.filter(vigente=True, cartao=c.fase2).values_list("id", flat=True)) == antes
    assert Resultado.objects.filter(vigente=True, cartao=c.cartao, execucao=exe).count() == 1
    assert exe.status == "OK"


def test_ajuste_manual_sobrevive_ao_recalculo_e_nao_muda_a_trava(c):
    l = c.lanc(date(2026, 7, 9), "48.00", "RESTAURANTE")
    c.rodar()
    r = Resultado.objects.get(vigente=True, fatura_lanc=l)
    assert r.status == "SEM_FORMULARIO"
    AjusteManual.objects.create(competencia=c.comp, chave=chave_resultado(r), status="CONCILIADO",
                                observacao="nota física conferida", usuario="ana")
    exe, res = c.rodar()
    novo = res.get(fatura_lanc=l)
    assert (novo.status, novo.status_automatico) == ("CONCILIADO", "SEM_FORMULARIO")
    assert novo.ajuste is not None and "nota física conferida" in novo.motivo and "ana" in novo.motivo
    assert exe.status == "OK" and D(exe.resumo["diferenca"]) == 0


def test_ajuste_vale_para_nova_versao_da_fatura(c):
    l = c.lanc(date(2026, 7, 9), "48.00", "RESTAURANTE")
    c.rodar()
    r = Resultado.objects.get(vigente=True, fatura_lanc=l)
    AjusteManual.objects.create(competencia=c.comp, chave=chave_resultado(r), status="PENDENTE_ANALISE")
    # Nova versão da fatura: outro objeto de lançamento, mesma chave natural.
    c.fatura.ativo = False
    c.fatura.save()
    c.fatura = Importacao.objects.create(tipo="FATURA", competencia=c.comp, versao=2, origem="UPLOAD", status="OK",
                                         ativo=True)
    Competencia.objects.filter(pk=c.comp.pk).update(total_fatura=D("0"))
    c.comp.refresh_from_db()
    c.ordem = 0
    l2 = c.lanc(date(2026, 7, 9), "48.00", "RESTAURANTE")
    l2.n_ocorrencia = l.n_ocorrencia
    l2.save()
    _, res = c.rodar()
    assert res.get(fatura_lanc=l2).status == "PENDENTE_ANALISE"


def test_comprovante_anexado_tira_a_ressalva_sem_recibo(c):
    l = c.lanc(date(2026, 7, 2), "55.00", "PADARIA")
    f = c.form(date(2026, 7, 2), "55.00", "Alimentação", link="foto.jpg")
    _, res = c.rodar()
    assert res.get(fatura_lanc=l).status == "CONCILIADO_RESSALVA"
    Anexo.objects.create(competencia=c.comp, chave=chave_despesa(f), arquivo_caminho="/x", nome_original="nota.pdf",
                         mime="application/pdf", hash_sha256="0" * 64, tamanho=1)
    _, res = c.rodar()
    r = res.get(fatura_lanc=l)
    assert r.status == "CONCILIADO" and not r.sem_comprovante


def test_planilha_de_outro_periodo_tambem_e_usada(c):
    """Compra de julho que só aparece na fatura de setembro: casa com a resposta da planilha de julho."""
    set_ = Competencia.objects.create(ano_mes="2026-09", periodo_ini=date(2026, 8, 3), periodo_fim=date(2026, 9, 1),
                                      fechamento=date(2026, 9, 1), vencimento=date(2026, 9, 11), total_fatura=D("0"))
    fat_set = Importacao.objects.create(tipo="FATURA", competencia=set_, versao=1, origem="UPLOAD", status="OK",
                                        ativo=True)
    Importacao.objects.create(tipo="PLANILHA", representante=c.rep, competencia=set_, versao=1, origem="UPLOAD",
                              status="OK", ativo=True)
    c.form(date(2026, 7, 31), "88.00", "Combustível")          # está na planilha de julho (c.planilha)
    lanc = FaturaLanc.objects.create(importacao=fat_set, cartao=c.cartao, secao="PORTADOR", natureza="COMPRA",
                                     data=date(2026, 7, 31), descricao="POSTO", valor=D("88.00"), ordem=1)
    Competencia.objects.filter(pk=set_.pk).update(total_fatura=D("88.00"))
    set_.refresh_from_db()
    exe = motor.executar(set_)
    assert Resultado.objects.get(execucao=exe, fatura_lanc=lanc).status == "CONCILIADO"


def test_sem_planilha_do_periodo_aguarda_mesmo_com_planilha_de_outro_mes(c):
    c.planilha.competencia = None
    c.planilha.ativo = False
    c.planilha.save()
    outro = Competencia.objects.create(ano_mes="2026-07", periodo_ini=date(2026, 6, 1), periodo_fim=date(2026, 7, 1),
                                       fechamento=date(2026, 6, 30), vencimento=date(2026, 7, 11), total_fatura=D("0"))
    Importacao.objects.create(tipo="PLANILHA", representante=c.rep, competencia=outro, versao=1, origem="UPLOAD",
                              status="OK", ativo=True)
    l = c.lanc(date(2026, 7, 1), "15.00")
    _, r = c.rodar()
    assert _status(r, lanc=l) == "AGUARDANDO_PLANILHA"


def test_competencia_fechada_nao_recalcula(c):
    Competencia.objects.filter(pk=c.comp.pk).update(status="FECHADA")
    c.comp.refresh_from_db()
    with pytest.raises(motor.ConciliacaoBloqueada):
        motor.executar(c.comp)
    assert not Execucao.objects.exists()


def test_cartao_de_notas_fiscais_concilia_com_a_nota_anexada(c):
    compra = c.lanc(date(2026, 7, 14), "384.00", "WIX", cartao=c.fase2)
    credito = c.lanc(date(2026, 7, 15), "-50.00", "DEVOLUCAO", cartao=c.fase2)
    fora = Cartao.objects.create(final="5504", nome_fatura="JOAO", escopo="FORA_ESCOPO")
    outro = c.lanc(date(2026, 7, 16), "10.00", "X", cartao=fora)
    _, r = c.rodar()
    assert (_status(r, lanc=compra), _status(r, lanc=credito), _status(r, lanc=outro)) == (
        "SEM_NOTA", "PENDENTE_ANALISE", "FORA_ESCOPO")
    Anexo.objects.create(competencia=c.comp, chave=chave_lancamento(compra), arquivo_caminho="/x",
                         nome_original="nf.pdf", mime="application/pdf", hash_sha256="0" * 64, tamanho=1)
    exe, r = c.rodar()
    linha = r.get(fatura_lanc=compra)
    assert linha.status == "CONCILIADO" and "nota fiscal anexada" in linha.motivo
    assert exe.status == "OK"
