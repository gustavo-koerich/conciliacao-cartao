"""Registro de alterações da Conciliação (tabela de auditoria): quem fez o quê, quando e de onde. Só administradores."""
from django.core.paginator import Paginator
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .models import Auditoria

POR_PAGINA = 100


def _admin(request):
    acesso = getattr(request, "usuario_acesso", None)
    return bool((acesso or {}).get("admin")) or bool(getattr(request.user, "is_staff", False))


def _detalhe(d):
    if not isinstance(d, dict):
        return str(d or "")
    return " · ".join(f"{k}: {v}" for k, v in d.items() if v not in (None, "", [], {}))[:300]


@require_GET
def registro(request):
    if not _admin(request):
        return render(request, "conciliacao/bloqueado.html",
                      {"motivo": "O registro de alterações é só para administradores da Central."}, status=403)
    f = request.GET
    itens = Auditoria.objects.select_related("competencia", "representante").order_by("-momento")
    if f.get("usuario"):
        itens = itens.filter(usuario__icontains=f["usuario"].strip())
    if f.get("acao"):
        itens = itens.filter(acao=f["acao"])
    if f.get("de"):
        itens = itens.filter(momento__date__gte=f["de"])
    if f.get("ate"):
        itens = itens.filter(momento__date__lte=f["ate"])
    pagina = Paginator(itens, POR_PAGINA).get_page(f.get("pagina"))
    for a in pagina:
        a.detalhe_texto = _detalhe(a.detalhe)
    sem_pagina = f.copy()
    sem_pagina.pop("pagina", None)
    return render(request, "conciliacao/registro.html", {
        "pagina": pagina, "acoes": Auditoria.Acao.choices, "f": f, "query": sem_pagina.urlencode(),
        "usuarios": Auditoria.objects.exclude(usuario="").values_list("usuario", flat=True).distinct().order_by("usuario"),
    })
