from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def moeda(valor):
    """1234.5 → "R$ 1.234,50"; negativo → "-R$ 1.234,50"; vazio → "—"."""
    if valor in (None, ""):
        return "—"
    try:
        numero = Decimal(str(valor))
    except InvalidOperation:
        return valor
    texto = f"{abs(numero):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if numero < 0 else ''}R$ {texto}"


@register.filter
def porcento(parte, todo):
    try:
        return f"{100 * int(parte) / int(todo):.0f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return "—"


@register.filter
def get_item(dicionario, chave):
    return dicionario.get(chave, "") if hasattr(dicionario, "get") else ""


@register.filter
def split(texto, separador):
    return str(texto).split(separador)


@register.filter
def vezes(valor, fator):
    try:
        return Decimal(str(valor)) * int(fator)
    except (InvalidOperation, TypeError, ValueError):
        return ""


@register.filter
def situacao(status):
    """Nome da situação como aparece na tela (ex.: SEM_FORMULARIO -> "Sem justificativa")."""
    from conciliacao.models import rotulo_situacao
    return rotulo_situacao(status)
