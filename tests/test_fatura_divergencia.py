"""Fatura que não fecha: a pessoa decide se a diferença é relevante (seguir) ou manda o relatório ao suporte."""
from datetime import date
from decimal import Decimal as D

import pytest

from conciliacao.fatura import servico
from conciliacao.fatura.interpretacao import FaturaLida, Lancamento, Secao
from conciliacao.models import Auditoria, ContaCartao, Importacao, Resultado, conta_padrao

pytestmark = pytest.mark.django_db


def _fatura(total_impresso="100.00", valores=("60.00", "40.00"), declarado=None):
    secao = Secao(final="5501", nome="FABIO TESTE", pagina_ini=1, pagina_fim=1,
                  total_declarado=D(declarado or total_impresso))
    for i, v in enumerate(valores, start=1):
        secao.lancamentos.append(Lancamento("PORTADOR", "COMPRA", date(2026, 7, 10 + i), f"LOJA {i}", "", D(v), 1, ordem=i))
    secao.diferenca = secao.soma - secao.total_declarado
    secao.ok = secao.diferenca == 0
    return FaturaLida(metodo="TEXTO", periodo_ini=date(2026, 7, 2), periodo_fim=date(2026, 8, 1),
                      fechamento=date(2026, 8, 3), vencimento=date(2026, 8, 12),
                      total_cabecalho=D(total_impresso), portadores=[secao])


def _importar(fat, nome="fatura.pdf", hash_="a" * 64):
    imp = Importacao.objects.create(tipo=Importacao.Tipo.FATURA, conta=ContaCartao.objects.get(pk=conta_padrao()),
                                    status=Importacao.Status.PROCESSANDO, arquivo_caminho="/tmp/x.pdf",
                                    arquivo_nome_original=nome, hash_sha256=hash_, usuario="ana")
    return servico._gravar(imp, fat)


def test_diferenca_mostra_pergunta_e_relatorio(client):
    imp = _importar(_fatura(valores=("60.00", "39.99")))
    assert imp.status == "ERRO" and not imp.ativo and servico.pode_ignorar_divergencia(imp)
    pagina = client.get(f"/fatura/{imp.pk}/").content.decode()
    assert "Essa diferença é relevante?" in pagina and "Relatório para o suporte" in pagina
    rel = client.get(f"/fatura/{imp.pk}/suporte/")
    texto = rel.content.decode()
    assert rel["Content-Disposition"].startswith("attachment") and "diferença -0.01" in texto and "fatura.pdf" in texto


def test_ignorar_diferenca_coloca_em_uso_e_registra_o_motivo(client):
    imp = _importar(_fatura(valores=("60.00", "39.99")))
    assert client.post(f"/fatura/{imp.pk}/ignorar/", {"justificativa": "x"}).status_code == 302
    imp.refresh_from_db()
    assert not imp.ativo                                         # motivo curto demais
    client.post(f"/fatura/{imp.pk}/ignorar/", {"justificativa": "centavo de arredondamento do IOF"})
    imp.refresh_from_db()
    assert imp.ativo and imp.status == "OK"
    assert imp.resumo["divergencia_aceita"]["justificativa"] == "centavo de arredondamento do IOF"
    assert Auditoria.objects.filter(importacao=imp, acao="ATUALIZACAO").exists()
    assert Resultado.objects.filter(competencia=imp.competencia, vigente=True).count() == 2
    assert "Em uso com diferença" in client.get(f"/fatura/{imp.pk}/").content.decode()


def test_erro_de_leitura_nao_tem_opcao_de_ignorar(client):
    imp = Importacao.objects.create(tipo=Importacao.Tipo.FATURA, status=Importacao.Status.ERRO,
                                    arquivo_caminho="/tmp/x.pdf", arquivo_nome_original="ruim.pdf", hash_sha256="b" * 64,
                                    resumo={"erro": "Não encontrei o cabeçalho da fatura."})
    assert not servico.pode_ignorar_divergencia(imp)
    pagina = client.get(f"/fatura/{imp.pk}/").content.decode()
    assert "Não foi possível ler a fatura" in pagina and "Relatório para o suporte" in pagina
    assert "Ignorar a diferença" not in pagina


def test_versao_nova_so_entra_quando_confirmada(client):
    v1 = _importar(_fatura())
    v2 = _importar(_fatura(valores=("70.00", "30.00")), hash_="c" * 64)
    assert v1.ativo and not v2.ativo and v2.resumo["aguardando_confirmacao"]
    assert "Usar esta versão" in client.get(f"/fatura/{v2.pk}/").content.decode()
    client.post(f"/fatura/{v2.pk}/usar/")
    v1.refresh_from_db()
    v2.refresh_from_db()
    assert v2.ativo and not v1.ativo and "aguardando_confirmacao" not in v2.resumo


# ---------- erro na leitura do resumo (cabeçalho) ----------

from conciliacao.fatura.interpretacao import ErroCabecalho, cabecalho_parcial, ler_cabecalho  # noqa: E402

RESUMO_SEM_REF = ("FECHAMENTO DA FATURA 03/08/2026 VENCIMENTO: 12 AGO 2026 VALOR TOTAL DE R$ 100,00 "
                  "REF: 1 JUL A 1 A60")   # "AGO" lido como "A60": período não reconhecido


def test_resumo_ilegivel_traz_o_que_deu_para_ler():
    with pytest.raises(ErroCabecalho):
        ler_cabecalho(RESUMO_SEM_REF)
    p = cabecalho_parcial(RESUMO_SEM_REF)
    assert p == {"total": D("100.00"), "fechamento": date(2026, 8, 3), "vencimento": date(2026, 8, 12)}


def _processar_com_erro_de_resumo(monkeypatch):
    chamadas = []

    def ler_falso(conta, caminho, progresso, forcar_ocr, informado=None):
        chamadas.append(informado)
        if informado is None:
            exc = ErroCabecalho("Não encontrei no resumo da fatura: período (REF).")
            exc.parcial, exc.trecho = cabecalho_parcial(RESUMO_SEM_REF), RESUMO_SEM_REF
            raise exc
        fat = _fatura()
        fat.periodo_ini, fat.periodo_fim = informado["periodo_ini"], informado["periodo_fim"]
        return fat

    monkeypatch.setattr(servico, "_ler", ler_falso)
    from conciliacao import tarefas
    monkeypatch.setattr(tarefas, "enfileirar_fatura", lambda pk: servico.processar(pk))
    imp = Importacao.objects.create(tipo=Importacao.Tipo.FATURA, conta=ContaCartao.objects.get(pk=conta_padrao()),
                                    status=Importacao.Status.PROCESSANDO, arquivo_caminho="/tmp/x.pdf",
                                    arquivo_nome_original="outubro.pdf", hash_sha256="d" * 64)
    return servico.processar(imp.pk), chamadas


def test_erro_no_resumo_pergunta_e_segue_com_os_dados_conferidos(client, monkeypatch):
    imp, chamadas = _processar_com_erro_de_resumo(monkeypatch)
    assert imp.status == "ERRO" and servico.pode_informar_cabecalho(imp)
    pagina = client.get(f"/fatura/{imp.pk}/").content.decode()
    assert "Esse erro é relevante?" in pagina and 'value="2026-08-03"' in pagina and "não lido" in pagina
    assert "A60" in servico.relatorio_suporte(imp)                     # o suporte vê o texto como foi lido
    dados = {"periodo_ini": "2026-07-02", "periodo_fim": "2026-08-01", "fechamento": "2026-08-03",
             "vencimento": "2026-08-12", "total": "100,00"}
    client.post(f"/fatura/{imp.pk}/cabecalho/", dados | {"vencimento": "2026-07-01"})   # vencimento antes: recusa
    imp.refresh_from_db()
    assert imp.status == "ERRO" and len(chamadas) == 1
    client.post(f"/fatura/{imp.pk}/cabecalho/", dados)
    imp.refresh_from_db()
    assert imp.status == "OK" and imp.ativo and imp.competencia.periodo_ini == date(2026, 7, 2)
    assert chamadas[1]["total"] == D("100.00") and imp.resumo["cabecalho_informado"]["usuario"] == ""
    assert "Resumo da fatura informado" in client.get(f"/fatura/{imp.pk}/").content.decode()


def test_erro_que_nao_e_do_resumo_so_tem_relatorio(client):
    imp = Importacao.objects.create(tipo=Importacao.Tipo.FATURA, status=Importacao.Status.ERRO,
                                    arquivo_caminho="/tmp/x.pdf", arquivo_nome_original="ruim.pdf", hash_sha256="e" * 64,
                                    resumo={"erro": "Nenhuma página com a tabela de lançamentos foi reconhecida."})
    assert not servico.pode_informar_cabecalho(imp)
    assert "Esse erro é relevante?" not in client.get(f"/fatura/{imp.pk}/").content.decode()


def test_ler_de_novo_cria_versao_nova_que_substitui_a_aceita_com_diferenca(client, monkeypatch):
    """Fatura aceita com diferença (leitor antigo); depois da correção do leitor, "Ler de novo" fecha e substitui."""
    imp = _importar(_fatura(valores=("60.00", "39.99")))
    servico.ignorar_divergencia(imp, "erro na leitura da fatura")
    from conciliacao import tarefas
    monkeypatch.setattr(servico, "_ler", lambda *a, **k: _fatura())       # leitor corrigido: agora fecha
    monkeypatch.setattr(tarefas, "enfileirar_fatura", lambda pk: servico.processar(pk))
    resp = client.post(f"/fatura/{imp.pk}/reler/")
    nova = Importacao.objects.filter(tipo="FATURA").exclude(pk=imp.pk).get()
    assert resp["Location"].endswith(f"/fatura/{nova.pk}/")
    assert nova.status == "OK" and not nova.ativo and nova.resumo["aguardando_confirmacao"]
    assert nova.hash_sha256 == imp.hash_sha256 and nova.versao == imp.versao + 1
    client.post(f"/fatura/{nova.pk}/usar/")
    nova.refresh_from_db()
    imp.refresh_from_db()
    assert nova.ativo and not imp.ativo and nova.resumo["soma_lida"] == nova.resumo["total_fatura"]
