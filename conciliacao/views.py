import logging

from django.contrib import messages
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from . import tarefas
from .fatura import servico as fatura_servico
from .models import ContaCartao, FaturaLanc, FaturaSecao, Importacao
from .rede import nome_maquina

log = logging.getLogger(__name__)


def healthz(request):
    """Usado pelo healthcheck do Docker. Fica fora do controle de acesso e não expõe dados."""
    try:
        connection.ensure_connection()
    except Exception:  # noqa: BLE001
        log.error("healthz: banco indisponível", exc_info=True)
        return JsonResponse({"ok": False}, status=503)
    return JsonResponse({"ok": True})


def inicio(request):
    return redirect("conciliacao:painel")


def _usuario(request):
    return (getattr(request, "usuario_acesso", None) or {}).get("usuario", "")


@require_http_methods(["GET", "POST"])
def fatura(request):
    if request.method == "POST":
        arquivo = request.FILES.get("arquivo")
        conta = ContaCartao.objects.filter(pk=request.POST.get("conta"), ativo=True).first()
        if not arquivo or not conta:
            messages.error(request, "Escolha o banco e o arquivo da fatura.")
            return redirect("conciliacao:fatura")
        try:
            imp, criada = fatura_servico.criar_importacao(
                arquivo, arquivo.name, conta=conta, usuario=_usuario(request), ip=request.ip_cliente,
                maquina=nome_maquina(request.ip_cliente))
        except fatura_servico.ArquivoRecusado as exc:
            messages.error(request, str(exc))
            return redirect("conciliacao:fatura")
        if not criada:
            messages.info(request, f"Este arquivo é o mesmo da versão em uso ({imp.competencia} v{imp.versao}). "
                                   "Nada foi alterado.")
        else:
            tarefas.enfileirar_fatura(imp.pk)
        return redirect("conciliacao:fatura_detalhe", pk=imp.pk)

    historico = (Importacao.objects.filter(tipo=Importacao.Tipo.FATURA)
                 .select_related("competencia__conta", "conta").order_by("-criado_em")[:20])
    contas = list(ContaCartao.objects.filter(ativo=True))
    for c in contas:
        c.formatos = ", ".join(e.lstrip(".").upper() for e in fatura_servico.FORMATOS[c.leitor])
    return render(request, "conciliacao/fatura.html", {"historico": historico, "contas": contas})


ROTULOS_CABECALHO = {"periodo_ini": "Início do período", "periodo_fim": "Fim do período",
                     "fechamento": "Fechamento", "vencimento": "Vencimento", "total": "Total da fatura (R$)"}


def _campos_cabecalho(imp):
    """Campos do formulário, já preenchidos com o que deu para ler."""
    lido = (imp.resumo or {}).get("cabecalho_parcial") or {}
    return [{"nome": c, "rotulo": ROTULOS_CABECALHO[c], "valor": lido.get(c, ""),
             "tipo": "text" if c == "total" else "date", "lido": c in lido}
            for c in fatura_servico.campos_cabecalho(imp)]


def _contexto_detalhe(imp, secao_id=None):
    secoes = list(imp.secoes.select_related("cartao__representante").order_by("pagina_ini", "id"))
    lancamentos = None
    secao_aberta = None
    if secao_id:
        secao_aberta = next((s for s in secoes if str(s.pk) == str(secao_id)), None)
        if secao_aberta:
            filtro = {"cartao": secao_aberta.cartao} if secao_aberta.cartao else {"secao": FaturaLanc.Secao.CONTA}
            lancamentos = imp.lancamentos.filter(**filtro).order_by("ordem")
    erro = imp.status == Importacao.Status.ERRO
    return {"imp": imp, "r": imp.resumo or {}, "secoes": secoes, "secao_aberta": secao_aberta,
            "lancamentos": lancamentos,
            "com_erro": [s for s in secoes if s.status == FaturaSecao.Status.ERRO_LEITURA],
            "pode_ignorar": erro and fatura_servico.pode_ignorar_divergencia(imp),
            "pode_usar": fatura_servico.pode_usar_versao(imp),
            "pode_informar": fatura_servico.pode_informar_cabecalho(imp),
            "campos_cabecalho": _campos_cabecalho(imp) if fatura_servico.pode_informar_cabecalho(imp) else [],
            "relatorio": fatura_servico.relatorio_suporte(imp) if erro else ""}


def fatura_detalhe(request, pk):
    imp = get_object_or_404(Importacao, pk=pk, tipo=Importacao.Tipo.FATURA)
    return render(request, "conciliacao/fatura_detalhe.html", _contexto_detalhe(imp, request.GET.get("secao")))


@require_http_methods(["POST"])
def fatura_ignorar(request, pk):
    """A diferença não é relevante: segue com o carregamento (fica registrado quem, quando e por quê)."""
    imp = get_object_or_404(Importacao, pk=pk, tipo=Importacao.Tipo.FATURA)
    try:
        fatura_servico.ignorar_divergencia(imp, request.POST.get("justificativa", ""), usuario=_usuario(request),
                                           ip=request.ip_cliente)
    except fatura_servico.AtivacaoRecusada as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Fatura em uso, com a diferença registrada. A conciliação foi refeita.")
    return redirect("conciliacao:fatura_detalhe", pk=imp.pk)


@require_http_methods(["POST"])
def fatura_cabecalho(request, pk):
    """A leitura do resumo falhou e não é relevante: a pessoa confere/corrige os dados e a leitura recomeça."""
    imp = get_object_or_404(Importacao, pk=pk, tipo=Importacao.Tipo.FATURA)
    try:
        fatura_servico.informar_cabecalho(imp, request.POST, usuario=_usuario(request), ip=request.ip_cliente)
    except fatura_servico.AtivacaoRecusada as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Dados conferidos. A fatura está sendo lida de novo com eles.")
    return redirect("conciliacao:fatura_detalhe", pk=imp.pk)


@require_http_methods(["POST"])
def fatura_reler(request, pk):
    """Lê o mesmo arquivo de novo com o leitor atual (nova versão)."""
    imp = get_object_or_404(Importacao, pk=pk, tipo=Importacao.Tipo.FATURA)
    try:
        nova = fatura_servico.reler(imp, usuario=_usuario(request), ip=request.ip_cliente)
    except fatura_servico.AtivacaoRecusada as exc:
        messages.error(request, str(exc))
        return redirect("conciliacao:fatura_detalhe", pk=imp.pk)
    tarefas.enfileirar_fatura(nova.pk)
    messages.success(request, "Lendo o arquivo de novo. Se fechar, use \"Usar esta versão\" para trocar a que está em uso.")
    return redirect("conciliacao:fatura_detalhe", pk=nova.pk)


@require_http_methods(["POST"])
def fatura_usar(request, pk):
    """Coloca a versão nova no lugar da que está em uso."""
    imp = get_object_or_404(Importacao, pk=pk, tipo=Importacao.Tipo.FATURA)
    try:
        fatura_servico.usar_versao(imp, usuario=_usuario(request), ip=request.ip_cliente)
    except fatura_servico.AtivacaoRecusada as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Versão {imp.versao} em uso. A conciliação foi refeita.")
    return redirect("conciliacao:fatura_detalhe", pk=imp.pk)


def fatura_suporte(request, pk):
    """Relatório do erro em .txt, para mandar ao suporte."""
    imp = get_object_or_404(Importacao, pk=pk, tipo=Importacao.Tipo.FATURA)
    resp = HttpResponse(fatura_servico.relatorio_suporte(imp), content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="suporte-fatura-{imp.pk}.txt"'
    return resp


def fatura_parcial(request, pk):
    """Atualizado pelo HTMX a cada 2 s enquanto a leitura roda."""
    imp = get_object_or_404(Importacao, pk=pk, tipo=Importacao.Tipo.FATURA)
    return render(request, "conciliacao/_fatura_resultado.html", _contexto_detalhe(imp, request.GET.get("secao")))


def ajuda(request):
    return render(request, "conciliacao/ajuda.html")


def ajuda_pdf(request):
    return render(request, "conciliacao/ajuda_impressao.html", {"versao": timezone.localdate().strftime("%d/%m/%Y")})


def ajuda_representante(request):
    return render(request, "conciliacao/ajuda_representante.html")
