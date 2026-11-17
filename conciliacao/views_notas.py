"""Tela de notas fiscais por competência: envio em lote (XML, PDF ou .zip), casamento e associação manual."""
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import controle, motor, notas
from .arquivos import ArquivoRecusado
from .models import Anexo, Cartao, Competencia, Importacao, NotaFiscal, Resultado, chave_lancamento
from .rede import nome_maquina
from .views_conciliacao import _competencia, seletor_competencias


def _usuario(request):
    return (getattr(request, "usuario_acesso", None) or {}).get("usuario", "")


def tela(request, codigo):
    comp = _competencia(codigo)
    pendentes = notas.lancamentos_pendentes(comp)
    chaves = {chave_lancamento(l): l for l in pendentes}
    resultados = {chave_lancamento(r.fatura_lanc): r for r in Resultado.objects.filter(
        competencia=comp, vigente=True, fatura_lanc__in=pendentes).select_related("fatura_lanc__cartao")}
    # Registrada no controle sem NF já vai como conciliado com ressalva: sai da lista de "sem nota".
    linhas_pendentes = [resultados[k] for k in chaves if k in resultados and resultados[k].status == "SEM_NOTA"]
    casadas = (Resultado.objects.filter(competencia=comp, vigente=True, cartao__escopo=Cartao.Escopo.FASE2_NF,
                                        status__in=["CONCILIADO", "CONCILIADO_RESSALVA"])
               .select_related("fatura_lanc", "cartao").order_by("fatura_lanc__data"))
    competencias = list(Competencia.objects.select_related("conta").order_by("-periodo_ini"))
    # Nota casada pelo sistema pode ser desfeita (casamento só pelo valor pode estar errado).
    anexos = {a.chave: a for a in Anexo.objects.filter(competencia=comp, ativo=True, nota__isnull=False)}
    casadas = list(casadas)
    for r in casadas:
        r.anexo_nota = anexos.get(chave_lancamento(r.fatura_lanc))
    sem_nota = [r for r in casadas if r.sem_comprovante]
    casadas = [r for r in casadas if not r.sem_comprovante]
    outras = list(notas.notas_sem_lancamento())
    return render(request, "conciliacao/notas.html", {
        "comp": comp, "pendentes": linhas_pendentes, "casadas": casadas, "sem_nota": sem_nota,
        "associaveis": linhas_pendentes + sem_nota,
        "outras": outras[:500], "total_outras": len(outras), "voltar": request.path,
        "seletor": seletor_competencias(competencias, comp), "fechada": comp.fechada,
        "controle": Importacao.objects.filter(tipo=Importacao.Tipo.CONTROLE, ativo=True).first()})


@require_POST
def enviar_controle(request, codigo):
    """Planilha CONTROLE DE COMPRAS CARTÃO: vale para todos os bancos; a versão nova substitui a anterior."""
    comp = _competencia(codigo)
    arquivo = request.FILES.get("controle")
    if arquivo is None:
        messages.error(request, "Escolha a planilha de controle de compras (.xlsx).")
        return redirect("conciliacao:notas", codigo=comp.codigo)
    usuario = _usuario(request)
    try:
        imp, nova = controle.importar(arquivo, arquivo.name, usuario=usuario, ip=request.ip_cliente,
                                      maquina=nome_maquina(request.ip_cliente))
    except (ArquivoRecusado, controle.ControleInvalido) as exc:
        messages.error(request, str(exc))
        return redirect("conciliacao:notas", codigo=comp.codigo)
    if not nova:
        messages.info(request, "Esta planilha de controle já está em uso.")
        return redirect("conciliacao:notas", codigo=comp.codigo)
    # Refaz as competências abertas dos cartões de notas fiscais e tenta casar as notas pelo número informado.
    cartoes = list(Cartao.objects.filter(escopo=Cartao.Escopo.FASE2_NF))
    for c in Competencia.objects.filter(status=Competencia.Status.ABERTA, importacoes__tipo=Importacao.Tipo.FATURA,
                                        importacoes__ativo=True).distinct():
        motor.executar(c, [x for x in cartoes if x.conta_id == c.conta_id], motivo="controle de compras", usuario=usuario)
    casadas = notas.casar_abertas(usuario=usuario or "automático")
    r = imp.resumo
    messages.success(request, f"Controle de compras em uso: {r['linhas']} compra(s), {r['com_nf']} com NF informada"
                              f"; {casadas} nota(s) casada(s) pelo número.")
    return redirect("conciliacao:notas", codigo=comp.codigo)


@require_POST
def enviar(request, codigo):
    comp = _competencia(codigo)
    enviados = request.FILES.getlist("arquivos")
    if not enviados:
        messages.error(request, "Escolha as notas (XML, PDF ou .zip).")
        return redirect("conciliacao:notas", codigo=comp.codigo)
    try:
        imp = notas.importar(enviados, comp, usuario=_usuario(request), ip=request.ip_cliente,
                             maquina=nome_maquina(request.ip_cliente))
    except ArquivoRecusado as exc:
        messages.error(request, str(exc))
        return redirect("conciliacao:notas", codigo=comp.codigo)
    r = imp.resumo
    messages.success(request, f"{r['lidas']} nota(s) lida(s), {r['casadas']} casada(s) com a fatura"
                              + (f", {r['nao_lidas']} não lida(s) (associe à mão)" if r["nao_lidas"] else "")
                              + (f", {r['repetidas']} repetida(s) ignorada(s) (mesma nota já no sistema ou duplicada no envio)"
                                 if r["repetidas"] else "")
                              + (f"; {r['pdf_com_xml']} PDF(s) ignorado(s) porque a mesma nota veio em XML"
                                 if r.get("pdf_com_xml") else "") + ".")
    return redirect("conciliacao:notas", codigo=comp.codigo)


@require_POST
def casar_de_novo(request, codigo):
    comp = _competencia(codigo)
    if comp.fechada:
        raise PermissionDenied("Competência fechada.")
    n = notas.casar(comp, usuario=_usuario(request) or "automático")
    messages.success(request, f"{n} nota(s) casada(s)." if n else "Nenhuma nota nova casou com a fatura.")
    return redirect("conciliacao:notas", codigo=comp.codigo)


@require_POST
def associar(request, codigo, nota_pk):
    comp = _competencia(codigo)
    if comp.fechada:
        raise PermissionDenied("Competência fechada.")
    nota = get_object_or_404(NotaFiscal, pk=nota_pk)
    resultado = get_object_or_404(Resultado.objects.select_related("fatura_lanc__cartao", "competencia"),
                                  pk=request.POST.get("resultado"), competencia=comp, vigente=True,
                                  cartao__escopo=Cartao.Escopo.FASE2_NF)
    notas.associar(nota, resultado, usuario=_usuario(request), ip=request.ip_cliente)
    messages.success(request, f"{nota} associada a {resultado.fatura_lanc.descricao} "
                              f"({resultado.fatura_lanc.data:%d/%m}).")
    return redirect("conciliacao:notas", codigo=comp.codigo)
