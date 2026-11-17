"""Telas da análise de gastos: dashboard e lista geral (com exportação e importação da base geral)."""
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import analise, auditoria, parametros, periodo
from .arquivos import ArquivoRecusado
from .models import Importacao
from .parametros import normalizar_texto
from .rede import nome_maquina

POR_PAGINA = 100


def _usuario(request):
    return (getattr(request, "usuario_acesso", None) or {}).get("usuario", "")


def dashboard(request):
    coletado = analise.coletar()
    return render(request, "conciliacao/analise.html", {"dados": analise.dados_dashboard(coletado),
                                                        "sem_dados": not coletado["linhas"]})


def _filtrar(linhas, g, de, ate):
    """de/ate: "AAAA-MM" já validados (mês da FATURA, ver periodo.py)."""
    rep, cat, pag, fonte = g.get("rep", ""), g.get("categoria", ""), g.get("pagamento", ""), g.get("fonte", "")
    busca = normalizar_texto(g.get("q", ""))
    saida = []
    for l in linhas:
        mes = l["mes"]
        if rep and l["rep"] != rep or cat and l["categoria"] != cat or fonte and l["fonte"] != fonte:
            continue
        if pag and analise._pagamento(l["pagamento"]) != pag:
            continue
        if mes < de or mes > ate:
            continue
        if busca and busca not in normalizar_texto(" ".join(str(l[k]) for k in (
                "descricao", "cidade", "medico", "paciente", "despesa", "finalidade", "evento"))):
            continue
        saida.append(l)
    return saida


def lista(request):
    coletado = analise.coletar()
    todas = coletado["linhas"]
    erro_periodo = ""
    try:
        de, ate = periodo.ler_periodo(request.GET.get("de", ""), request.GET.get("ate", ""))
    except periodo.PeriodoInvalido as exc:
        erro_periodo = str(exc)
        de = ate = periodo.ler_mes(periodo.mes_atual(), "De")
    filtradas = _filtrar(todas, request.GET, de, ate)
    if request.GET.get("formato") == "xlsx":
        conteudo = analise.exportar_xlsx(filtradas)
        auditoria.registrar(request, "EXPORTACAO", "OK",
                            detalhe={"exportacao": "lista geral", "linhas": len(filtradas),
                                     "filtros": {k: v for k, v in request.GET.items() if k != "formato" and v}})
        resposta = HttpResponse(conteudo, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resposta["Content-Disposition"] = f'attachment; filename="lista-geral-gastos-{timezone.localdate():%Y%m%d}.xlsx"'
        return resposta
    pagina = Paginator(filtradas, POR_PAGINA).get_page(request.GET.get("pagina"))
    parametros_sem_pagina = request.GET.copy()
    parametros_sem_pagina.pop("pagina", None)
    return render(request, "conciliacao/analise_lista.html", {
        "pagina": pagina, "totais": analise.totais(filtradas), "qtd": len(filtradas),
        "de": periodo.texto(de), "ate": periodo.texto(ate), "erro_periodo": erro_periodo, "fontes": analise.FONTES,
        "reps": sorted({l["rep"] for l in todas}), "categorias": parametros.CATEGORIAS_ANALISE,
        "f": request.GET, "query": parametros_sem_pagina.urlencode(),
        "base": coletado["base"], "substituidas": coletado["substituidas_pela_planilha"],
        "versoes_base": Importacao.objects.filter(tipo=Importacao.Tipo.BASE).order_by("-criado_em")[:10],
    })


@require_POST
def importar_base(request):
    arquivo = request.FILES.get("arquivo")
    if not arquivo:
        messages.error(request, "Escolha a planilha geral (.xlsx).")
        return redirect("conciliacao:analise_lista")
    try:
        imp, criada = analise.importar_base(arquivo, arquivo.name, usuario=_usuario(request), ip=request.ip_cliente,
                                            maquina=nome_maquina(request.ip_cliente))
    except (ArquivoRecusado, analise.BaseInvalida) as exc:
        messages.error(request, str(exc))
        return redirect("conciliacao:analise_lista")
    if not criada:
        messages.info(request, "Este arquivo é o mesmo da base em uso. Nada foi alterado.")
    else:
        r = imp.resumo
        ignoradas = ", ".join(f"{v} {k}" for k, v in r["ignoradas"].items() if v) or "nenhuma"
        criados = f" Representantes novos no cadastro: {', '.join(r['representantes_criados'])}." \
            if r["representantes_criados"] else ""
        messages.success(request, f"Base geral v{imp.versao} em uso: {r['linhas']} despesas (linhas ignoradas: "
                                  f"{ignoradas}).{criados}")
    return redirect("conciliacao:analise_lista")
