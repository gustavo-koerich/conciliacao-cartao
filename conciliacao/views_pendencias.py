"""Portal de pendências, lado do financeiro: gerar o link do representante, revogar, aprovar e recusar respostas."""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import pendencias
from .models import LinkPendencias, Representante, RespostaPendencia, Resultado, chave_lancamento
from .views_conciliacao import _competencia, _resultado_editavel, _usuario, _voltar
from .views_registro import _admin


@require_POST
def gerar(request, codigo, rep_id):
    comp = _competencia(codigo)
    rep = get_object_or_404(Representante, pk=rep_id)
    try:
        dias = int(request.POST.get("dias", "1"))
        link, token = pendencias.gerar_link(rep, comp, dias, usuario=_usuario(request), ip=request.ip_cliente)
    except (ValueError, pendencias.PendenciaInvalida) as exc:
        messages.error(request, str(exc) if isinstance(exc, pendencias.PendenciaInvalida) else "Validade inválida.")
        return redirect("conciliacao:representante", codigo=comp.codigo, rep_id=rep.pk)
    texto = pendencias.mensagem(link, token)
    # O código só existe agora: a página mostra o link uma única vez (no banco fica só a impressão).
    return render(request, "conciliacao/link_gerado.html", {
        "comp": comp, "rep": rep, "link": link, "url": pendencias.url_do_link(token), "mensagem": texto,
        "whatsapp": pendencias.link_whatsapp(texto), "sem_endereco": not pendencias.url_do_link(token).startswith("http")})


@require_POST
def revogar(request, pk):
    link = get_object_or_404(LinkPendencias.objects.select_related("competencia", "representante"), pk=pk)
    pendencias.revogar(link, usuario=_usuario(request), ip=request.ip_cliente)
    messages.success(request, "Link revogado: ele não abre mais.")
    return redirect("conciliacao:representante", codigo=link.competencia.codigo, rep_id=link.representante_id)


@require_POST
def excluir_teste(request, pk):
    if not _admin(request):
        raise PermissionDenied("Só administradores excluem dados de teste.")
    link = get_object_or_404(LinkPendencias.objects.select_related("competencia", "representante"), pk=pk)
    codigo, rep_id = link.competencia.codigo, link.representante_id
    try:
        r = pendencias.excluir_teste(link, usuario=_usuario(request), ip=request.ip_cliente)
    except pendencias.PendenciaInvalida as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Teste excluído: link, {r['respostas']} resposta(s) e {r['comprovantes']} "
                                  "comprovante(s). Fica só uma linha no registro.")
    return redirect("conciliacao:representante", codigo=codigo, rep_id=rep_id)


def _resposta_da_linha(pk):
    r = _resultado_editavel(pk)
    if r.fatura_lanc is None:
        raise PermissionDenied("Linha sem lançamento da fatura.")
    resp = RespostaPendencia.objects.filter(chave=chave_lancamento(r.fatura_lanc), ativo=True).select_related(
        "link__competencia", "link__representante").first()
    if resp is None:
        raise PermissionDenied("Esta linha não tem resposta do representante.")
    return r, resp


@require_POST
def aprovar(request, pk):
    r, resp = _resposta_da_linha(pk)
    pendencias.aprovar(resp, usuario=_usuario(request), ip=request.ip_cliente)
    messages.success(request, "Resposta aprovada: a linha ficou conciliada.")
    return _voltar(request, r.competencia, r.representante, r)


@require_POST
def recusar(request, pk):
    r, resp = _resposta_da_linha(pk)
    try:
        pendencias.recusar(resp, request.POST.get("motivo", ""), usuario=_usuario(request), ip=request.ip_cliente)
    except pendencias.PendenciaInvalida as exc:
        messages.error(request, str(exc))
        return _voltar(request, r.competencia, r.representante, r)
    messages.success(request, "Resposta recusada: o representante vê o motivo e pode responder de novo pelo link.")
    return _voltar(request, r.competencia, r.representante, r)
