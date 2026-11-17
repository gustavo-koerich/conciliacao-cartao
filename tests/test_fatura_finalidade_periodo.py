"""Regras de 26/10/2026: a fatura prevalece nos totais, período MM/AAAA pelo mês da fatura, Finalidade/Evento."""
from datetime import date
from decimal import Decimal as D

import pytest
from django.db import IntegrityError, transaction

from conciliacao import analise, eventos, periodo
from conciliacao.models import (Auditoria, CorrecaoValor, Evento, FinalidadeLancamento, Resultado, chave_despesa,
                                chave_lancamento)
from tests.test_motor import Cenario

pytestmark = pytest.mark.django_db
IP = "10.0.0.8"


# ---------- 1. a FATURA é a fonte da verdade ----------

def test_totais_usam_o_valor_da_fatura_e_separam_reembolso_e_nao_encontrado():
    c = Cenario()
    casado = c.form(date(2026, 7, 2), "100.00")
    c.lanc(date(2026, 7, 2), "100.00", "RESTAURANTE")
    c.lanc(date(2026, 7, 5), "50.00", "UBER")                         # sem formulário: "Sem justificativa"
    c.form(date(2026, 7, 6), "30.00", pagamento="Reembolso")          # pago pelo representante
    c.form(date(2026, 7, 7), "70.00")                                  # não está na fatura
    c.rodar()
    linhas = analise.coletar()["linhas"]
    t = analise.totais(linhas)
    assert t == {"fatura": D("150.00"), "reembolso": D("30.00"), "nao_encontrado": D("70.00")}
    assert t["fatura"] == c.comp.total_fatura                          # fecha com o total da fatura
    sem = next(l for l in linhas if l["descricao"] == "UBER")
    assert (sem["situacao"], sem["conta_total"], sem["origem"]) == ("Sem justificativa", True, "Sem justificativa")
    fora = next(l for l in linhas if l["fonte"] == "Não encontrado na fatura")
    assert fora["conta_total"] is False and fora["situacao"] == "Não encontrado na fatura"
    assert next(l for l in linhas if l["ref"].endswith(f"F{casado.pk}"))["valor"] == D("100.00")


def test_divergente_quando_informado_difere_da_fatura_sem_explicacao():
    c = Cenario()
    f = c.form(date(2026, 7, 2), "100.00")
    lanc = c.lanc(date(2026, 7, 2), "100.00", "RESTAURANTE")
    c.rodar()
    # valor informado corrigido depois do casamento (sem recalcular): a fatura continua valendo
    CorrecaoValor.objects.create(chave=chave_despesa(f), representante=c.rep, valor_original=D("100.00"),
                                 valor_corrigido=D("120.00"), motivo="teste")
    l = next(x for x in analise.coletar()["linhas"] if x["ref"] == f"L{lanc.pk}F{f.pk}")
    assert (l["valor"], l["valor_informado"], l["diferenca"], l["divergente"]) == (
        D("100.00"), D("120.00"), D("20.00"), True)


def test_parcelado_vale_a_parcela_da_fatura_e_nao_e_divergente():
    c = Cenario()
    c.form(date(2026, 7, 5), "1000.00", "Manutenção veicular")
    lanc = c.lanc(date(2026, 7, 5), "100.00", "PNEUS", parcela=(1, 10))
    c.rodar()
    l = next(x for x in analise.coletar()["linhas"] if x["ref"].startswith(f"L{lanc.pk}F"))
    assert (l["valor"], l["valor_informado"], l["divergente"]) == (D("100.00"), D("1000.00"), False)


def test_conta_dividida_soma_exatamente_o_valor_da_fatura():
    c = Cenario()
    c.form(date(2026, 7, 5), "32.00")
    c.form(date(2026, 7, 5), "351.60")
    lanc = c.lanc(date(2026, 7, 5), "383.60", "POSTO")
    c.rodar()
    partes = [x for x in analise.coletar()["linhas"] if x["ref"].startswith(f"L{lanc.pk}F")]
    assert len(partes) == 2 and sum(p["valor"] for p in partes) == lanc.valor
    assert not any(p["divergente"] for p in partes)


def test_pagina_do_representante_mostra_diferenca_e_rotulos_novos(client):
    c = Cenario()
    c.lanc(date(2026, 7, 5), "50.00", "UBER")
    c.form(date(2026, 7, 6), "30.00", pagamento="Reembolso")
    c.rodar()
    pagina = client.get(f"/painel/{c.comp.codigo}/representante/{c.rep.pk}/", REMOTE_ADDR=IP).content.decode()
    assert "Sem justificativa" in pagina and "Reembolsado (fora da fatura)" in pagina and "R$ 30,00" in pagina


# ---------- 2. período MM/AAAA (mês da fatura) ----------

def test_periodo_mm_aaaa_validacao(settings):
    assert periodo.ler_periodo("06/2026", "10/2026") == ("2026-06", "2026-10")
    assert periodo.ler_periodo("11/2025", "02/2027") == ("2025-11", "2027-02")      # vários anos
    hoje = periodo.ler_mes(periodo.mes_atual(), "De")
    assert periodo.ler_periodo("", "") == (hoje, hoje)                              # padrão: mês atual
    for de, ate, trecho in (("13/2026", "10/2026", "01 a 12"), ("6-2026", "10/2026", "MM/AAAA"),
                            ("06/26", "10/2026", "MM/AAAA"), ("10/2026", "06/2026", "não pode ser depois"),
                            ("00/2026", "01/2026", "01 a 12")):
        with pytest.raises(periodo.PeriodoInvalido, match=trecho):
            periodo.ler_periodo(de, ate)


def test_lista_filtra_pelo_mes_da_fatura_e_mostra_erro(client):
    c = Cenario()                                    # fatura 2026-08 com compra de julho
    c.lanc(date(2026, 7, 5), "50.00", "UBER")
    c.rodar()
    pagina = client.get("/analise/lista/?de=08/2026&ate=08/2026", REMOTE_ADDR=IP).content.decode()
    assert "UBER" in pagina                                           # julho na compra, agosto na fatura
    assert "UBER" not in client.get("/analise/lista/?de=07/2026&ate=07/2026", REMOTE_ADDR=IP).content.decode()
    erro = client.get("/analise/lista/?de=13/2026&ate=08/2026", REMOTE_ADDR=IP).content.decode()
    assert "o mês vai de 01 a 12" in erro and 'value="' in erro


# ---------- 3 e 4. Finalidade e Evento ----------

def test_eventos_iniciais_da_migration():
    nomes = set(Evento.objects.values_list("nome", flat=True))
    assert len(nomes) == 16 and {"EVENTO AORTA", "EVENTO MILÃO", "EVENTO VEITH - NY"} <= nomes


def test_normaliza_e_bloqueia_evento_duplicado():
    assert eventos.normalizar_nome("  carioca   novo ") == "EVENTO CARIOCA NOVO"
    assert eventos.normalizar_nome("evento   teste") == "EVENTO TESTE"
    e = eventos.cadastrar(" encontro  sul ", usuario="suelen")
    assert e.nome == "EVENTO ENCONTRO SUL" and e.criado_por == "suelen"
    for repetido in ("Evento Encontro Sul", "ENCONTRO SUL", "evento milao", "Evento Milão"):
        with pytest.raises(eventos.EscolhaInvalida, match="Já existe"):
            eventos.cadastrar(repetido)
    with pytest.raises(eventos.EscolhaInvalida):
        eventos.cadastrar("   ")


def _linha():
    c = Cenario()
    lanc = c.lanc(date(2026, 7, 5), "50.00", "HOTEL")
    c.rodar()
    return c, lanc, Resultado.objects.get(vigente=True, fatura_lanc=lanc)


def test_evento_so_com_finalidade_evento():
    c, lanc, r = _linha()
    chave = chave_lancamento(lanc)
    aorta = Evento.objects.get(nome="EVENTO AORTA")
    with pytest.raises(eventos.EscolhaInvalida, match="escolha o evento"):
        eventos.escolher(r, chave, "EVENTO", "")
    escolha = eventos.escolher(r, chave, "EVENTO", str(aorta.pk), usuario="suelen")
    assert (escolha.finalidade, escolha.evento_id) == ("EVENTO", aorta.pk)
    # mudou a finalidade: o evento é limpo, a escolha anterior fica guardada (inativa)
    escolha = eventos.escolher(r, chave, "NEGOCIACAO", str(aorta.pk), usuario="suelen")
    assert (escolha.finalidade, escolha.evento) == ("NEGOCIACAO", None)
    assert FinalidadeLancamento.objects.filter(chave=chave).count() == 2
    assert FinalidadeLancamento.objects.filter(chave=chave, ativo=True).count() == 1
    # e o banco recusa evento sem finalidade EVENTO (e EVENTO sem evento)
    with pytest.raises(IntegrityError), transaction.atomic():
        FinalidadeLancamento.objects.create(competencia=c.comp, chave="x", finalidade="PROCEDIMENTO", evento=aorta)
    with pytest.raises(IntegrityError), transaction.atomic():
        FinalidadeLancamento.objects.create(competencia=c.comp, chave="y", finalidade="EVENTO")


def test_tela_cadastra_evento_novo_e_registra_auditoria(client):
    c, lanc, r = _linha()
    resp = client.post(f"/resultado/{r.pk}/finalidade/", {"finalidade": "EVENTO", "evento": "novo",
                                                           "novo_evento": "congresso sul"}, REMOTE_ADDR=IP, follow=True)
    assert resp.status_code == 200
    escolha = FinalidadeLancamento.objects.get(chave=chave_lancamento(lanc), ativo=True)
    assert escolha.evento.nome == "EVENTO CONGRESSO SUL"
    assert Auditoria.objects.filter(acao="CADASTRO", detalhe__evento="EVENTO CONGRESSO SUL").exists()
    linha = next(l for l in analise.coletar()["linhas"] if l["ref"] == f"L{lanc.pk}")
    assert (linha["finalidade"], linha["evento"]) == ("EVENTO", "EVENTO CONGRESSO SUL")
    # duplicado pela tela: mensagem e nada novo
    resp = client.post(f"/resultado/{r.pk}/finalidade/", {"finalidade": "EVENTO", "evento": "novo",
                                                           "novo_evento": "Evento Congresso Sul"}, REMOTE_ADDR=IP,
                       follow=True)
    assert "Já existe o evento" in resp.content.decode() and Evento.objects.filter(nome__contains="SUL").count() == 1
