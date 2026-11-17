"""Planilha GERAL REPRESENTANTES: todos os representantes numa aba, distribuída por competência."""
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from conciliacao.models import FormDespesa, Importacao, Representante, Resultado, chave_despesa
from conciliacao.planilha import servico
from tests.test_motor import Cenario

pytestmark = pytest.mark.django_db


def _geral(tmp_path, linhas, nome="GERAL REPRESENTANTES.xlsx"):
    caminho = tmp_path / nome
    df = pd.DataFrame([{"Nome da Origem": n, "Mês": "JULHO", "Data": datetime.fromisoformat(d), "Despesa": desp,
                        "Descrição": "x", "Finalidade": "Visita", "Pagamento": pag, "Médico": "", "Paciente": "",
                        "Cidade": "SP", "Valor": v, "Ano": 2026} for n, d, desp, pag, v in linhas])
    with pd.ExcelWriter(caminho) as w:
        pd.DataFrame({"x": [1]}).to_excel(w, sheet_name="Dashboard", index=False)
        df.to_excel(w, sheet_name="ATUALIZADOS", index=False)
    return caminho


def test_distribui_por_representante_e_concilia(tmp_path):
    c = Cenario()                                       # Fábio, cartão 5501, Ailos 2026-08 (02/07 a 01/08)
    l1 = c.lanc(date(2026, 7, 9), "40.00", "ESTACIONAMENTO")
    l2 = c.lanc(date(2026, 7, 9), "40.00", "ESTACIONAMENTO")
    l3 = c.lanc(date(2026, 7, 20), "99.00", "LOJA")
    c.planilha.ativo = False                            # sem a planilha do Forms
    c.planilha.save()
    arq = _geral(tmp_path, [("FABIO", "2026-07-09", "Estacionamento", "Cartão Crédito", 40),
                            ("FABIO", "2026-07-09", "Estacionamento", "Cartão Crédito", 40),   # igual e real
                            ("FABIO", "2026-09-01", "Alimentação", "Cartão Crédito", 10),     # outro período
                            ("MARIA NOVA", "2026-07-10", "Alimentação", "Reembolso", 30)])
    resumo = servico.importar_geral(arq, arq.name, [c.comp], usuario="t")
    assert resumo["competencias"][str(c.comp)][c.rep.nome] == 2
    assert "Maria Nova" in resumo["criados"]
    v = Resultado.objects.filter(vigente=True)
    # a planilha geral é conferida pelo financeiro: vale como comprovada (verde)
    assert v.get(fatura_lanc=l1).status == v.get(fatura_lanc=l2).status == "CONCILIADO"
    assert v.get(fatura_lanc=l3).status == "SEM_FORMULARIO"
    assert not FormDespesa.objects.filter(duplicado=True).exists()


def test_chave_estavel_entre_envios_e_versao_anterior_inativa(tmp_path):
    c = Cenario()
    linhas = [("FABIO", "2026-07-09", "Estacionamento", "Cartão Crédito", 40),
              ("FABIO", "2026-07-09", "Estacionamento", "Cartão Crédito", 40)]
    servico.importar_geral(_geral(tmp_path, linhas), "g1.xlsx", [c.comp])
    chaves1 = sorted(chave_despesa(d) for d in FormDespesa.objects.filter(importacao__ativo=True))
    servico.importar_geral(_geral(tmp_path, list(reversed(linhas)) + [("FABIO", "2026-07-11", "Táxi", "Cartão Crédito", 25)],
                                  "g2.xlsx"), "g2.xlsx", [c.comp])
    ativas = Importacao.objects.filter(tipo="PLANILHA", representante=c.rep, competencia=c.comp, ativo=True)
    assert ativas.count() == 1 and ativas.get().versao == 3          # 1 = Forms do cenário, 2 e 3 = geral
    chaves2 = {chave_despesa(d) for d in FormDespesa.objects.filter(importacao__ativo=True)}
    assert set(chaves1) <= chaves2 and len(chaves1) == 2               # mesmas linhas, mesmas chaves


def test_quem_concilia_e_nao_tem_linhas_no_periodo_fica_sem_formulario(tmp_path):
    c = Cenario()
    l = c.lanc(date(2026, 7, 9), "55.00", "RESTAURANTE")
    c.planilha.ativo = False
    c.planilha.save()
    servico.importar_geral(_geral(tmp_path, [("FABIO", "2026-09-10", "Alimentação", "Cartão Crédito", 10)]),
                           "g.xlsx", [c.comp])
    assert Resultado.objects.get(vigente=True, fatura_lanc=l).status == "SEM_FORMULARIO"


def test_nome_repetido_no_cadastro_vai_para_quem_tem_cartao(tmp_path):
    c = Cenario()
    Representante.objects.create(nome=c.rep.nome)                  # duplicado sem cartão
    servico.importar_geral(_geral(tmp_path, [("FABIO", "2026-07-09", "Táxi", "Cartão Crédito", 12)]), "g.xlsx",
                           [c.comp])
    assert FormDespesa.objects.get(valor=Decimal("12.00")).representante_id == c.rep.pk


def test_envio_pela_tela_enfileira(client, tmp_path, monkeypatch):
    from conciliacao import tarefas
    c = Cenario()
    c.fatura.ativo = True
    c.fatura.save()
    pedidos = []
    monkeypatch.setattr(tarefas, "enfileirar_geral", lambda *a, **k: pedidos.append(a))
    arq = _geral(tmp_path, [("FABIO", "2026-07-09", "Táxi", "Cartão Crédito", 12)])
    pagina = client.get(f"/planilhas/{c.comp.codigo}/", REMOTE_ADDR="10.0.0.8").content.decode()
    assert "Planilha geral" in pagina and f'value="{c.comp.pk}" checked' in pagina
    with open(arq, "rb") as fh:
        resp = client.post(f"/planilhas/{c.comp.codigo}/geral/", {"arquivo": fh, "competencias": [str(c.comp.pk)]},
                           REMOTE_ADDR="10.0.0.8")
    assert resp.status_code == 302 and pedidos and pedidos[0][2] == [c.comp.pk]
