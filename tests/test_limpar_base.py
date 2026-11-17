"""limpar_base: zera o movimento e mantém os cadastros; sem --confirmar não faz nada."""
import pytest
from django.core.management import CommandError, call_command

from conciliacao.models import Cartao, Competencia, FaturaLanc, Importacao, Representante

pytestmark = pytest.mark.django_db


def test_limpar_base_mantem_cadastros():
    from tests.test_motor import Cenario
    c = Cenario()
    c.lanc(c.comp.periodo_ini, "10.00")
    with pytest.raises(CommandError):
        call_command("limpar_base")
    assert FaturaLanc.objects.exists()
    call_command("limpar_base", confirmar=True)
    assert not FaturaLanc.objects.exists() and not Importacao.objects.exists() and not Competencia.objects.exists()
    assert Representante.objects.filter(pk=c.rep.pk).exists() and Cartao.objects.filter(pk=c.cartao.pk).exists()
