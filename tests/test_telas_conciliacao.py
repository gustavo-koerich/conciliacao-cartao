"""Telas de planilhas (por período), painel, detalhe do representante e ajustes manuais."""
from datetime import date, timedelta

import pandas as pd
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from conciliacao import tarefas
from conciliacao.models import AjusteManual, Anexo, Auditoria, Importacao, Resultado
from conciliacao.planilha import servico as planilha_servico
from tests.test_motor import Cenario

pytestmark = pytest.mark.django_db
IP = "10.0.0.8"
PDF = b"%PDF-1.4\n%teste\n"


@pytest.fixture
def fila(monkeypatch):
    enviados = []
    monkeypatch.setattr(tarefas, "enfileirar_planilha", lambda pk: enviados.append(pk))
    return enviados


def _xlsx(tmp_path, linhas):
    caminho = tmp_path / "fabio.xlsx"
    # Carimbo = dia seguinte à despesa (como na vida real: preenche depois de gastar).
    pd.DataFrame([{"Carimbo de data/hora": f"{date.fromisoformat(data) + timedelta(days=1)} 10:0{i}:00", "Data": data, "Despesa": "Estacionamento",
                   "Descrição": "x", "Finalidade": "y", "Pagamento": "Cartão Crédito", "Médico": "Dr A",
                   "Paciente": "Fulano de Tal", "Cidade": "SP", "Valor": valor, "Foto do Recibo/NF": "https://d/1"}
                  for i, (data, valor) in enumerate(linhas)]).to_excel(caminho, index=False)
    return caminho


def test_painel_e_planilhas_sem_dados(client):
    assert "Nenhuma competência" in client.get("/painel/", REMOTE_ADDR=IP).content.decode()
    assert "Nenhuma competência" in client.get("/planilhas/", REMOTE_ADDR=IP).content.decode()


def test_planilha_por_periodo_pela_tela(client, fila, tmp_path):
    c = Cenario()
    c.planilha.ativo = False
    c.planilha.save()
    c.lanc(c.comp.periodo_ini, "15.00", "SKIPARK ESTACIONAMEN")
    assert client.get("/planilhas/", REMOTE_ADDR=IP)["Location"].endswith("/planilhas/ailos-2026-08/")
    pagina = client.get("/planilhas/ailos-2026-08/", REMOTE_ADDR=IP).content.decode()
    assert "Fábio" in pagina and "não enviada" in pagina

    # Planilha completa: uma resposta de julho (período) e uma de setembro (fora)
    arquivo = SimpleUploadedFile("fabio.xlsx", _xlsx(tmp_path, [("2026-07-01", 15), ("2026-09-10", 30)]).read_bytes())
    resp = client.post(f"/planilhas/ailos-2026-08/enviar/{c.rep.pk}/", {"arquivo": arquivo}, REMOTE_ADDR=IP)
    imp = Importacao.objects.get(tipo="PLANILHA", ip=IP)
    assert resp.status_code == 302 and fila == [imp.pk] and imp.competencia == c.comp
    assert "Lendo a planilha" in client.get(f"/planilha/{imp.pk}/", REMOTE_ADDR=IP).content.decode()

    imp = planilha_servico.processar(imp.pk)
    assert imp.resumo["linhas"] == 1 and imp.resumo["outros_periodos"] == 1
    parcial = client.get(f"/planilha/{imp.pk}/parcial/", REMOTE_ADDR=IP).content.decode()
    assert "hx-trigger" not in parcial and "1 respostas de outros períodos" in parcial

    painel = client.get("/painel/ailos-2026-08/", REMOTE_ADDR=IP).content.decode()
    assert "fecha com o total da fatura ao centavo" in painel and "1 de 1 do período" in painel
    detalhe = client.get(f"/painel/ailos-2026-08/representante/{c.rep.pk}/", REMOTE_ADDR=IP).content.decode()
    assert "SKIPARK" in detalhe and "Fulano de Tal" in detalhe and "linha-verde" in detalhe
    assert "Valor declarado" in detalhe


def test_planilha_sem_respostas_do_periodo_da_erro(client, fila, tmp_path):
    c = Cenario()
    imp, _ = planilha_servico.criar_importacao(_xlsx(tmp_path, [("2026-09-10", 30)]), "fabio.xlsx", c.rep, c.comp)
    imp = planilha_servico.processar(imp.pk)
    assert imp.status == "ERRO" and "Nenhuma das 1 respostas" in imp.resumo["erro"]


def test_planilha_que_nao_e_xlsx_e_recusada(client, fila):
    c = Cenario()
    arquivo = SimpleUploadedFile("fabio.xlsx", b"texto")
    resp = client.post(f"/planilhas/ailos-2026-08/enviar/{c.rep.pk}/", {"arquivo": arquivo}, REMOTE_ADDR=IP, follow=True)
    assert "planilha .xlsx" in resp.content.decode() and fila == []


def _linha_sem_formulario():
    c = Cenario()
    l = c.lanc(date(2026, 7, 9), "48.00", "RESTAURANTE")
    c.rodar()
    return c, Resultado.objects.get(vigente=True, fatura_lanc=l)


def test_alterar_situacao_pela_tela(client):
    c, r = _linha_sem_formulario()
    url = f"/painel/ailos-2026-08/representante/{c.rep.pk}/"
    pagina = client.get(url, REMOTE_ADDR=IP).content.decode()
    assert "linha-vermelha" in pagina and f'action="/resultado/{r.pk}/situacao/"' in pagina

    resp = client.post(f"/resultado/{r.pk}/situacao/", {"status": "CONCILIADO", "observacao": "nota conferida",
                                                         "voltar": url + "?status=SEM_FORMULARIO"}, REMOTE_ADDR=IP)
    ref = f"L{r.fatura_lanc_id}"
    assert resp.status_code == 302 and resp["Location"] == url + f"?status=SEM_FORMULARIO&linha={ref}"
    # mesmo com o filtro "Sem formulário", a linha alterada continua na tela, verde e destacada
    pagina = client.get(resp["Location"], REMOTE_ADDR=IP).content.decode()
    assert f'class="linha-verde destacada" id="r-{ref}"' in pagina
    novo = Resultado.objects.get(vigente=True, fatura_lanc=r.fatura_lanc)
    assert (novo.status, novo.status_automatico) == ("CONCILIADO", "SEM_FORMULARIO")
    assert Auditoria.objects.filter(acao="AJUSTE_MANUAL", ip=IP).exists()
    pagina = client.get(url, REMOTE_ADDR=IP).content.decode()
    assert "linha-verde" in pagina and "ajuste manual" in pagina and "voltar ao automático" in pagina

    client.post(f"/resultado/{novo.pk}/situacao/", {"status": "AUTOMATICO", "voltar": url}, REMOTE_ADDR=IP)
    assert Resultado.objects.get(vigente=True, fatura_lanc=r.fatura_lanc).status == "SEM_FORMULARIO"
    assert not AjusteManual.objects.filter(ativo=True).exists() and AjusteManual.objects.count() == 1


def test_situacao_invalida_e_redirecionamento_externo_sao_recusados(client):
    _, r = _linha_sem_formulario()
    resp = client.post(f"/resultado/{r.pk}/situacao/", {"status": "SEM_COBRANCA", "voltar": "https://exemplo.com/"},
                       REMOTE_ADDR=IP)
    assert resp["Location"].startswith("/painel/") and not AjusteManual.objects.exists()


def test_anexar_ver_e_remover_comprovante(client):
    c, r = _linha_sem_formulario()
    arquivo = SimpleUploadedFile("nota.pdf", PDF)
    resp = client.post(f"/resultado/{r.pk}/comprovante/", {"arquivo": arquivo}, REMOTE_ADDR=IP)
    assert resp.status_code == 302
    anexo = Anexo.objects.get()
    assert anexo.ip == IP and anexo.mime == "application/pdf" and anexo.nome_original == "nota.pdf"
    pagina = client.get(f"/painel/ailos-2026-08/representante/{c.rep.pk}/", REMOTE_ADDR=IP).content.decode()
    assert f"/comprovante/{anexo.pk}/" in pagina
    arquivo_servido = client.get(f"/comprovante/{anexo.pk}/", REMOTE_ADDR=IP)
    assert arquivo_servido.status_code == 200 and b"".join(arquivo_servido.streaming_content) == PDF
    client.post(f"/comprovante/{anexo.pk}/remover/", REMOTE_ADDR=IP)
    anexo.refresh_from_db()
    assert not anexo.ativo


def test_anexo_que_nao_e_pdf_nem_imagem_e_recusado(client):
    _, r = _linha_sem_formulario()
    resp = client.post(f"/resultado/{r.pk}/comprovante/", {"arquivo": SimpleUploadedFile("nota.pdf", b"MZ exe")},
                       REMOTE_ADDR=IP, follow=True)
    assert "PDF ou imagem" in resp.content.decode() and not Anexo.objects.exists()


def test_competencia_fechada_nao_aceita_ajuste(client):
    c, r = _linha_sem_formulario()
    c.comp.status = "FECHADA"
    c.comp.save()
    resp = client.post(f"/resultado/{r.pk}/situacao/", {"status": "CONCILIADO"}, REMOTE_ADDR=IP)
    assert resp.status_code == 403 and not AjusteManual.objects.exists()
