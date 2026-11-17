"""Cadastros pela tela (bancos, cartões, representantes), seletor banco/ano/mês e listas por fatura."""
from datetime import date

import pytest

from conciliacao.models import Auditoria, Cartao, ContaCartao, Representante, Resultado
from tests.test_motor import Cenario

pytestmark = pytest.mark.django_db
IP = "10.0.0.8"


def test_criar_e_editar_banco(client):
    resp = client.post("/cadastros/banco/", {"nova-nome": "Itaú Empresas", "nova-banco": "Itaú",
                                             "nova-leitor": "BB", "nova-ativo": "on"}, REMOTE_ADDR=IP, follow=True)
    conta = ContaCartao.objects.get(nome="Itaú Empresas")
    assert conta.slug == "itau" and conta.ativo and "Banco Itaú Empresas criado" in resp.content.decode()
    assert f'<option value="{conta.pk}"' in client.get("/fatura/", REMOTE_ADDR=IP).content.decode()
    client.post(f"/cadastros/banco/{conta.pk}/", {"nome": "Itaú Empresas", "banco": "Itaú", "leitor": "BB"},
                REMOTE_ADDR=IP)
    conta.refresh_from_db()
    assert not conta.ativo                          # checkbox desmarcado
    assert Auditoria.objects.filter(acao="CADASTRO", ip=IP).count() == 2
    assert f'<option value="{conta.pk}"' not in client.get("/fatura/", REMOTE_ADDR=IP).content.decode()


def test_banco_com_nome_repetido_e_recusado(client):
    ContaCartao.objects.create(nome="Itaú", banco="Itaú", slug="itau", leitor="BB")
    resp = client.post("/cadastros/banco/", {"nova-nome": "Itaú", "nova-banco": "Itaú", "nova-leitor": "BB"},
                       REMOTE_ADDR=IP, follow=True)
    assert "Banco não salvo" in resp.content.decode() and ContaCartao.objects.filter(nome="Itaú").count() == 1


def test_vincular_cartao_refaz_a_conciliacao(client):
    c = Cenario()
    sem_dono = Cartao.objects.create(final="7777", nome_fatura="NOVO PORTADOR", pendente_cadastro=True)
    l = c.lanc(date(2026, 7, 2), "30.00", "LOJA", cartao=sem_dono)
    c.rodar()
    assert Resultado.objects.get(vigente=True, fatura_lanc=l).status == "AGUARDANDO_PLANILHA"
    pagina = client.get("/cadastros/", REMOTE_ADDR=IP).content.decode()
    assert "7777" in pagina and "cadastro pendente" in pagina
    painel = client.get("/painel/ailos-2026-08/", REMOTE_ADDR=IP).content.decode()
    assert "Cartões desta fatura sem representante" in painel and "7777" in painel

    client.post(f"/cadastros/cartao/{sem_dono.pk}/", {"nome_fatura": "NOVO PORTADOR", "representante": c.rep.pk,
                                                      "escopo": "FASE2_NF"}, REMOTE_ADDR=IP)
    sem_dono.refresh_from_db()
    assert sem_dono.representante == c.rep and not sem_dono.pendente_cadastro
    assert Resultado.objects.get(vigente=True, fatura_lanc=l).status == "SEM_NOTA"


def test_novo_representante(client):
    client.post("/cadastros/representante/", {"novo-nome": "Heitor", "novo-email": "", "novo-ativo": "on"},
                REMOTE_ADDR=IP)
    assert Representante.objects.get(nome="Heitor").ativo


def test_planilhas_mostram_so_quem_esta_na_fatura(client):
    c = Cenario()
    Representante.objects.create(nome="Sem Cartão Nesta Fatura")
    c.lanc(date(2026, 7, 2), "30.00", "LOJA")
    pagina = client.get("/planilhas/ailos-2026-08/", REMOTE_ADDR=IP).content.decode()
    assert "Fábio" in pagina and "Sem Cartão Nesta Fatura" not in pagina
    assert 'id="dados-seletor"' in pagina and "agosto" in pagina
