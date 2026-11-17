"""Portal de pendências (o lado do representante): uma página com os gastos dele sem formulário, para responder pelo
celular. A autorização é o código secreto do link (ver pendencias.py); não há login."""
import logging

from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from . import parametros, pendencias
from .arquivos import ArquivoRecusado

log = logging.getLogger(__name__)


def _link_ou_erro(request, token):
    link = pendencias.buscar(token)
    if link is None:
        return None, render(request, "conciliacao/portal_aviso.html",
                            {"titulo": "Link não encontrado", "texto": "Confira se copiou o link inteiro ou peça um novo ao financeiro."},
                            status=404)
    if not link.valido:
        motivo = "foi cancelado" if link.revogado_em else "venceu"
        return None, render(request, "conciliacao/portal_aviso.html",
                            {"titulo": "Este link não vale mais", "texto": f"O link {motivo}. Peça um novo ao financeiro."},
                            status=410)
    return link, None


def _pagina(request, link, erro=None, erro_n=None, dados=None, status=200):
    lista = pendencias.itens(link)
    for i in lista:
        i["comprovantes"] = pendencias.comprovantes(i["chave"])
    feitos = sum(1 for i in lista if i["situacao"] in ("enviado", "conciliado"))
    return render(request, "conciliacao/portal.html", {
        "link": link, "itens": lista, "feitos": feitos, "categorias": list(parametros.CATEGORIAS_DESPESA),
        "erro": erro, "erro_n": erro_n, "dados": dados or {}, "salvo": request.GET.get("salvo")}, status=status)


@require_http_methods(["GET"])
def pagina(request, token):
    link, erro = _link_ou_erro(request, token)
    if erro:
        return erro
    pendencias.registrar_acesso(link)
    return _pagina(request, link)


@require_POST
def responder(request, token, n):
    link, erro = _link_ou_erro(request, token)
    if erro:
        return erro
    try:
        pendencias.responder(link, n, request.POST, request.FILES.get("comprovante"), ip=request.ip_cliente)
    except (pendencias.PendenciaInvalida, ArquivoRecusado) as exc:
        return _pagina(request, link, erro=str(exc), erro_n=n, dados=request.POST, status=400)
    return redirect(f"{request.path.rsplit('/', 2)[0]}/?salvo={n}#item-{n}")
