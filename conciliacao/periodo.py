"""Filtro de período DE / ATÉ digitado como MM/AAAA. O período é sempre o MÊS DA FATURA (competência), não a data
da compra: a fatura de outubro traz compras de setembro e às vezes de antes, e é ela que vale (regra de 26/10/2026)."""
import re

from django.utils import timezone

_MASCARA = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{4})\s*$")


class PeriodoInvalido(ValueError):
    """Mensagem já pronta para mostrar na tela."""


def mes_atual() -> str:
    """Mês atual em MM/AAAA (padrão dos dois campos)."""
    return timezone.localdate().strftime("%m/%Y")


def ler_mes(texto: str, campo: str) -> str:
    """MM/AAAA -> "AAAA-MM" (o formato interno, que ordena certo como texto)."""
    m = _MASCARA.match(texto or "")
    if not m:
        raise PeriodoInvalido(f"{campo}: use o formato MM/AAAA, por exemplo 06/2026.")
    mes, ano = int(m.group(1)), int(m.group(2))
    if not 1 <= mes <= 12:
        raise PeriodoInvalido(f"{campo}: o mês vai de 01 a 12 (veio {m.group(1)}).")
    if not 2000 <= ano <= 2100:
        raise PeriodoInvalido(f"{campo}: ano inválido ({ano}).")
    return f"{ano:04d}-{mes:02d}"


def ler_periodo(de: str, ate: str) -> tuple[str, str]:
    """Valida DE e ATÉ (vazio = mês atual) e devolve ("AAAA-MM", "AAAA-MM"). Aceita vários anos."""
    inicio = ler_mes(de or mes_atual(), "De")
    fim = ler_mes(ate or mes_atual(), "Até")
    if inicio > fim:
        raise PeriodoInvalido(f"O mês “De” ({de}) não pode ser depois do mês “Até” ({ate}).")
    return inicio, fim


def texto(mes_iso: str) -> str:
    """"AAAA-MM" -> "MM/AAAA"."""
    return f"{mes_iso[5:]}/{mes_iso[:4]}"
