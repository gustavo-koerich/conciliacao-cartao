"""Normalização da planilha do formulário (sem banco)."""
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from conciliacao import parametros
from conciliacao.planilha.normalizacao import PlanilhaInvalida, ler_valor, normalizar

PARAMS = {chave: item["padrao"] for chave, item in parametros.CATALOGO.items()}

CABECALHO = ["Carimbo de data/hora", "Data\n", "Despesa", "Descrição", "Finalidade", "Pagamento", "Médico",
             "Paciente", "Cidade", "Valor", "Foto do Recibo/NF", "Unnamed: 11", "Coluna 13"]


def _linha(carimbo, data, valor, despesa="Alimentação", pagamento="Cartão Crédito", medico="Dr X",
           link="https://drive.google.com/open?id=1"):
    return [carimbo, data, despesa, "desc", "fin", pagamento, medico, "Paciente Teste", "Cidade", valor, link,
            None, None]


def _planilha(tmp_path, linhas, aba="Respostas ao formulário 1"):
    caminho = tmp_path / "respostas.xlsx"
    with pd.ExcelWriter(caminho) as w:
        pd.DataFrame(linhas, columns=CABECALHO).to_excel(w, sheet_name=aba, index=False)
        pd.DataFrame().to_excel(w, sheet_name="Tabela dinâmica 1", index=False)
    return caminho


@pytest.mark.parametrize("bruto,esperado", [
    (45, Decimal("45.00")), (31.9, Decimal("31.90")), ("R$ 45,00", Decimal("45.00")), ("r$ 35,62", Decimal("35.62")),
    ("R$ 45", Decimal("45.00")), ("1.234,56", Decimal("1234.56")), ("12.5", Decimal("12.50")),
    ("1.180.00", Decimal("1180.00")), ("R$ 1.180.00", Decimal("1180.00")), ("1,180.00", Decimal("1180.00")),
    ("12.345.67", Decimal("12345.67")),
])
def test_valores_aceitos(bruto, esperado):
    assert ler_valor(bruto) == (esperado, False, False)


@pytest.mark.parametrize("bruto", ["130 po", "104,40.", "14/10", "Valor total 808,10 = 741,50 aluguel carro", None, 0,
                                   "1.180.0", "1.18.00", "1,180,00", "1.180,000", "1.180,180.00",
                                   "", -5])
def test_valores_invalidos_nao_sao_adivinhados(bruto):
    valor, invalido, _ = ler_valor(bruto)
    assert valor is None and invalido


def test_valor_em_dolar_vai_para_analise():
    assert ler_valor("350,00 Dólares ") == (None, True, True)


def test_normalizacao_completa(tmp_path):
    caminho = _planilha(tmp_path, [
        _linha(datetime(2026, 7, 6, 11, 0), datetime(2026, 7, 1), 15),
        _linha(datetime(2026, 7, 6, 11, 5), datetime(2002, 7, 3), 82.7),           # ano errado
        _linha(datetime(2026, 7, 6, 11, 6), "4/27/0026", 50),                       # texto americano, ano 0026
        _linha(datetime(2026, 10, 5, 9, 0), datetime(2026, 10, 28), 20),            # depois do carimbo
        _linha(datetime(2026, 7, 6, 11, 7), datetime(2026, 7, 1), 25, pagamento="Reembolso"),
        _linha(datetime(2026, 7, 6, 11, 8), datetime(2026, 7, 2), 30, pagamento="Cartão Crédito João"),
        _linha(datetime(2025, 9, 15, 13, 15), datetime(2025, 9, 15), 35, despesa="Estacionamento"),
        _linha(datetime(2025, 10, 6, 10, 6), datetime(2025, 9, 15), 35, despesa="Estacionamento"),  # duplicada
        _linha(datetime(2026, 7, 6, 11, 9), datetime(2026, 7, 2), "130 po", medico="  Sem  médico "),
        _linha(datetime(2026, 7, 6, 11, 10), datetime(2026, 7, 2), 10, link="foto.jpg"),
        [None] * len(CABECALHO),                                                    # linha vazia no meio
        _linha("2026-07-25 10:00:00", "2026-07-03", "R$ 12,00"),                    # texto ISO
    ])
    r = normalizar(caminho, PARAMS)
    d = {x.linha_planilha: x for x in r.despesas}
    assert len(r.despesas) == 11 and r.linhas_vazias == 1
    assert d[13].data_corrigida == date(2026, 7, 3) and d[13].carimbo.month == 7 and d[13].valor == Decimal("12.00")
    assert r.colunas_descartadas == ["Unnamed: 11", "Coluna 13"]

    assert d[2].data_corrigida == date(2026, 7, 1) and not d[2].data_suspeita
    assert d[3].data_suspeita and d[3].data_informada == date(2002, 7, 3) and d[3].data_corrigida == date(2026, 7, 3)
    assert d[4].data_suspeita and d[4].data_corrigida == date(2026, 4, 27)
    assert d[5].data_suspeita and d[5].data_corrigida is None                      # continua depois do carimbo
    assert d[7].pagamento_outro_cartao and not d[2].pagamento_outro_cartao
    assert not d[8].duplicado and d[9].duplicado                                    # a mais antiga vale
    assert d[10].valor_invalido and d[10].valor_original == "130 po"
    assert d[10].medico_canonico == "SEM MÉDICO" and d[2].medico_canonico == "Dr X"
    assert d[3].carimbo.tzinfo is not None
    c = r.contagens()
    assert (c["reembolso"], c["duplicado"], c["sem_link_valido"], c["valor_invalido"]) == (1, 1, 1, 1)


def test_planilha_sem_colunas_do_formulario_e_recusada(tmp_path):
    caminho = tmp_path / "outra.xlsx"
    pd.DataFrame({"JANEIRO": [1], "x": [2]}).to_excel(caminho, index=False)
    with pytest.raises(PlanilhaInvalida, match="colunas do formulário"):
        normalizar(caminho, PARAMS)


def test_arquivo_corrompido_e_recusado(tmp_path):
    caminho = tmp_path / "x.xlsx"
    caminho.write_bytes(b"PK\x03\x04lixo")
    with pytest.raises(PlanilhaInvalida, match="Não foi possível abrir"):
        normalizar(caminho, PARAMS)
