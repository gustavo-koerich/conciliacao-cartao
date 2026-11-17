"""Notas fiscais dos cartões de compras da empresa: leitura (XML/PDF), lote, casamento e associação manual."""
import io
import zipfile
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from conciliacao import notas
from conciliacao.models import Anexo, NotaFiscal, Resultado
from tests.test_motor import Cenario

pytestmark = pytest.mark.django_db
IP = "10.0.0.8"


def nfe(numero, emissao, valor, emitente="PARCOMED COMERCIO DE PRODUTOS LTDA", fantasia="", cnpj="12345678000199"):
    chave = f"4226{cnpj}55001{int(numero):09d}1000000019".ljust(44, "0")[:44]
    fant = f"<xFant>{fantasia}</xFant>" if fantasia else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?><nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">'
            f'<NFe><infNFe Id="NFe{chave}" versao="4.00"><ide><nNF>{numero}</nNF><serie>1</serie>'
            f'<dhEmi>{emissao}T10:15:00-03:00</dhEmi></ide><emit><CNPJ>{cnpj}</CNPJ><xNome>{emitente}</xNome>{fant}</emit>'
            f'<total><ICMSTot><vNF>{valor}</vNF></ICMSTot></total></infNFe></NFe></nfeProc>').encode()


def test_ler_xml_de_nfe():
    d = notas.ler_xml(nfe(1234, "2026-07-01", "11509.20", fantasia="PARCOMED"))
    assert d.lida and (d.numero, d.serie, d.emissao, d.valor, d.emitente, d.cnpj) == (
        "1234", "1", date(2026, 7, 1), Decimal("11509.20"), "PARCOMED", "12345678000199")
    assert len(d.chave) == 44


@pytest.mark.parametrize("conteudo,trecho", [
    (b"<nao fecha", "XML inválido"),
    (b"<NFSe><infNFSe/></NFSe>", "Não é um XML de NF-e"),
    (b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><l>&b;</l>', "XML inválido"),
])
def test_xml_que_nao_e_nfe_ou_e_perigoso_nao_e_lido(conteudo, trecho):
    d = notas.ler_xml(conteudo)
    assert not d.lida and trecho in d.erro


def test_texto_de_danfe_em_pdf():
    texto = ("RECEBEMOS DE FEDERAL EXPRESS CORP OS PRODUTOS CONSTANTES DA NOTA\nCHAVE DE ACESSO\n"
             "4226 0812 3456 7800 0199 5500 1000 0012 3410 0000 0019\nDATA DA EMISSÃO 22/09/2026\n"
             "VALOR TOTAL DA NOTA 2.536,04")
    d = notas.dados_do_texto_danfe(texto)
    assert d.lida and d.valor == Decimal("2536.04") and d.emissao == date(2026, 9, 22)
    assert d.numero == "1234" and d.emitente == "FEDERAL EXPRESS CORP" and len(d.chave) == 44


def _cenario():
    c = Cenario()
    c.fase2.representante = c.rep
    c.fase2.save()
    vista = c.lanc(date(2026, 7, 14), "384.00", "WIX COM", cartao=c.fase2)
    parcela = c.lanc(date(2026, 7, 1), "1918.20", "PARCOMED CURITIBA", cartao=c.fase2, parcela=(2, 6))
    c.rodar()
    return c, vista, parcela


def _enviar(c, *arquivos):
    return notas.importar([SimpleUploadedFile(n, b) for n, b in arquivos], c.comp, usuario="teste", ip=IP)


def test_lote_casa_a_vista_e_parcelada_e_deixa_o_resto(tmp_path):
    c, vista, parcela = _cenario()
    imp = _enviar(c, ("a.xml", nfe(10, "2026-07-16", "384.00", "WIX LTDA")),
                  ("b.xml", nfe(11, "2026-07-01", "11509.20")),          # 1.918,20 × 6
                  ("c.xml", nfe(12, "2026-07-20", "99.90", "OUTRA LOJA")))
    assert imp.resumo == {"lidas": 3, "nao_lidas": 0, "repetidas": 0, "pdf_com_xml": 0, "casadas": 2}
    vigentes = Resultado.objects.filter(vigente=True)
    assert vigentes.get(fatura_lanc=vista).status == vigentes.get(fatura_lanc=parcela).status == "CONCILIADO"
    assert [n.numero for n in notas.notas_sem_lancamento()] == ["12"]
    assert Anexo.objects.filter(automatico=True).count() == 2


def test_mesma_nota_nao_entra_duas_vezes_e_zip_funciona():
    c, _, _ = _cenario()
    _enviar(c, ("a.xml", nfe(10, "2026-07-16", "384.00")))
    pacote = io.BytesIO()
    with zipfile.ZipFile(pacote, "w") as z:
        z.writestr("pasta/a.xml", nfe(10, "2026-07-16", "384.00"))
        z.writestr("pasta/b.xml", nfe(13, "2026-07-02", "50.00"))
        z.writestr("pasta/leia-me.txt", "ignorar")
    imp = _enviar(c, ("notas.zip", pacote.getvalue()))
    assert (imp.resumo["lidas"], imp.resumo["repetidas"]) == (1, 1)
    assert NotaFiscal.objects.count() == 2


def test_nota_a_vista_com_data_muito_distante_nao_casa():
    c, vista, _ = _cenario()
    _enviar(c, ("a.xml", nfe(10, "2026-08-30", "384.00")))
    assert Resultado.objects.get(vigente=True, fatura_lanc=vista).status == "SEM_NOTA"


def test_tela_de_notas_e_associacao_manual(client):
    c, vista, _ = _cenario()
    resp = client.post(f"/notas/{c.comp.codigo}/enviar/",
                       {"arquivos": [SimpleUploadedFile("x.xml", nfe(20, "2026-09-01", "384.00", "SEM DATA PERTO"))]},
                       REMOTE_ADDR=IP, follow=True)
    pagina = resp.content.decode()
    assert "1 nota(s) lida(s), 0 casada(s)" in pagina and "SEM DATA PERTO" in pagina
    nota = NotaFiscal.objects.get()
    alvo = Resultado.objects.get(vigente=True, fatura_lanc=vista)
    client.post(f"/notas/{c.comp.codigo}/associar/{nota.pk}/", {"resultado": alvo.pk}, REMOTE_ADDR=IP)
    assert Resultado.objects.get(vigente=True, fatura_lanc=vista).status == "CONCILIADO"
    assert Anexo.objects.get(nota=nota).ip == IP
    painel = client.get(f"/painel/{c.comp.codigo}/", REMOTE_ADDR=IP).content.decode()
    assert "Notas fiscais" in painel


def test_arquivo_de_tipo_errado_e_recusado(client):
    c, _, _ = _cenario()
    resp = client.post(f"/notas/{c.comp.codigo}/enviar/", {"arquivos": [SimpleUploadedFile("x.exe", b"MZ")]},
                       REMOTE_ADDR=IP, follow=True)
    assert "envie XML, PDF ou um .zip" in resp.content.decode() and not NotaFiscal.objects.exists()


def test_novo_cartao_manual_no_banco(client):
    c = Cenario()
    client.post(f"/cadastros/banco/{c.cartao.conta_id}/cartao/", {"final": "4321", "nome_fatura": "FULANO",
                                                                  "novo_representante": "Heitor",
                                                                  "escopo": "CONCILIAR_FORMULARIO"}, REMOTE_ADDR=IP)
    from conciliacao.models import Cartao
    novo = Cartao.objects.get(final="4321")
    assert novo.representante.nome == "Heitor" and novo.conta == c.cartao.conta
    resp = client.post(f"/cadastros/banco/{c.cartao.conta_id}/cartao/", {"final": "4321", "nome_fatura": "X",
                                                                         "escopo": "FORA_ESCOPO"},
                       REMOTE_ADDR=IP, follow=True)
    assert "já tem um cartão com esse final" in resp.content.decode()


def test_so_pelo_valor_casa_com_ressalva_e_desfazer_nao_volta(client):
    c, vista, _ = _cenario()                                    # vista: "WIX COM" R$ 384,00 em 14/07
    imp = _enviar(c, ("a.xml", nfe(30, "2026-07-14", "384.00", "PURIFICAR COMERCIO E SERVICOS")))
    assert imp.resumo["casadas"] == 1
    r = Resultado.objects.get(vigente=True, fatura_lanc=vista)
    assert r.status == "CONCILIADO_RESSALVA" and "emitente não aparece" in r.motivo
    pagina = client.get(f"/notas/{c.comp.codigo}/", REMOTE_ADDR=IP).content.decode()
    assert "PURIFICAR" in pagina and "Desfazer" in pagina
    anexo = Anexo.objects.get(ativo=True, nota__isnull=False)
    client.post(f"/comprovante/{anexo.pk}/remover/", {"voltar": f"/notas/{c.comp.codigo}/"}, REMOTE_ADDR=IP)
    assert notas.casar(c.comp) == 0
    assert Resultado.objects.get(vigente=True, fatura_lanc=vista).status == "SEM_NOTA"


def test_so_pelo_valor_com_emissao_distante_fica_com_ressalva():
    c, vista, _ = _cenario()
    _enviar(c, ("a.xml", nfe(31, "2026-08-10", "384.00", "WIX LTDA")))     # 27 dias depois
    r = Resultado.objects.get(vigente=True, fatura_lanc=vista)
    assert r.status == "CONCILIADO_RESSALVA" and "27 dias" in r.motivo


def test_nota_com_nome_ganha_da_nota_so_pelo_valor():
    c, vista, _ = _cenario()
    _enviar(c, ("a.xml", nfe(32, "2026-07-14", "384.00", "PURIFICAR COMERCIO")),
            ("b.xml", nfe(33, "2026-07-15", "384.00", "WIX LTDA")))
    assert Resultado.objects.get(vigente=True, fatura_lanc=vista).status == "CONCILIADO"
    assert [n.numero for n in notas.notas_sem_lancamento()] == ["32"]


def test_pdf_com_o_mesmo_nome_do_xml_e_ignorado():
    c, _, _ = _cenario()
    imp = _enviar(c, ("WIX 10 384,00.xml", nfe(10, "2026-07-16", "384.00", "WIX LTDA")),
                  ("WIX 10 384,00.pdf", b"%PDF-1.4 qualquer"))
    assert (imp.resumo["lidas"], imp.resumo["pdf_com_xml"], imp.resumo["nao_lidas"]) == (1, 1, 0)
    assert NotaFiscal.objects.count() == 1
