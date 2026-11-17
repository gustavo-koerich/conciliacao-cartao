"""Páginas de ajuda: abrem para quem está logado, com o conteúdo e os links de impressão."""
import pytest

pytestmark = pytest.mark.django_db
IP = "10.0.0.8"


def test_ajuda_e_versoes_de_impressao(client):
    pagina = client.get("/ajuda/", REMOTE_ADDR=IP).content.decode()
    assert "Como usar a conciliação" in pagina and 'id="link"' in pagina and "Cartão do representante" in pagina
    impressao = client.get("/ajuda/imprimir/", REMOTE_ADDR=IP).content.decode()
    assert "@page" in impressao and "O que cada cor quer dizer" in impressao
    cartao = client.get("/ajuda/representante/", REMOTE_ADDR=IP).content.decode()
    assert "Tirar foto do comprovante" in cartao and "Enviado, aguardando o financeiro" in cartao
    assert 'href="/ajuda/"' in client.get("/painel/", REMOTE_ADDR=IP, follow=True).content.decode()
