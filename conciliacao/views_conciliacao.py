"""Telas de planilhas (upload por representante), painel da competência e detalhe do representante."""
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from . import arquivos, auditoria, avisos, eventos, motor, parametros, pendencias, tarefas
from .arquivos import ArquivoRecusado
from .models import (AjusteManual, Anexo, Backup, Cartao, Competencia, CorrecaoDespesa, CorrecaoValor, FaturaLanc,
                     FinalidadeLancamento, Importacao, LinkPendencias, Representante, RespostaPendencia, Resultado,
                     chave_despesa, chave_lancamento, chave_resultado, rotulo_situacao)
from .planilha import servico as planilha_servico
from .rede import nome_maquina

S = Resultado.Status
ZERO = Decimal("0")

# Grupos do painel (lado da fatura): a soma fecha o total da fatura.
GRUPOS = [
    ("conciliado", "Conciliado", "st-ok", [S.CONCILIADO, S.CONCILIADO_RESSALVA]),
    ("respondido", "Respondido, aguardando aprovação", "st-laranja", [S.AGUARDANDO_APROVACAO]),
    ("sem_formulario", "Sem justificativa", "st-semform", [S.SEM_FORMULARIO]),
    ("sem_nota", "Sem nota fiscal", "st-semform", [S.SEM_NOTA]),
    ("analise", "Pendente de análise", "st-ressalva", [S.PENDENTE_ANALISE]),
    ("aguardando", "Aguardando planilha", "st-neutro", [S.AGUARDANDO_PLANILHA]),
    ("estorno", "Estorno compensado", "st-info", [S.ESTORNO_COMPENSADO]),
    ("reembolso", "Reembolso", "st-info", [S.REEMBOLSO]),
    ("fora", "Fora do escopo", "st-neutro", [S.FORA_ESCOPO]),
    ("encargos", "Encargos da conta", "st-info", [S.ENCARGOS_CONTA]),
]
CLASSE_STATUS = {
    S.CONCILIADO: "st-ok", S.CONCILIADO_RESSALVA: "st-ressalva", S.SEM_FORMULARIO: "st-semform",
    S.SEM_COBRANCA: "st-semform", S.SEM_NOTA: "st-semform", S.PENDENTE_ANALISE: "st-ressalva", S.AGUARDANDO_PLANILHA: "st-neutro",
    S.DUPLICADO_FORM: "st-neutro", S.REEMBOLSO: "st-info", S.ESTORNO_COMPENSADO: "st-info",
    S.ENCARGOS_CONTA: "st-info", S.FORA_ESCOPO: "st-neutro", S.AGUARDANDO_APROVACAO: "st-laranja",
}


def _usuario(request):
    return (getattr(request, "usuario_acesso", None) or {}).get("usuario", "")


# ---------- planilhas ----------

def _competencia(codigo):
    try:
        return Competencia.por_codigo(codigo)
    except Competencia.DoesNotExist as exc:
        raise Http404("Competência não encontrada.") from exc


@require_http_methods(["GET"])
def planilhas(request, codigo=None):
    competencias = list(Competencia.objects.select_related("conta").order_by("-periodo_ini", "conta__nome"))
    if not competencias:
        return render(request, "conciliacao/planilhas.html", {"competencias": []})
    if codigo is None:
        return redirect("conciliacao:planilhas_competencia", codigo=competencias[0].codigo)
    comp = _competencia(codigo)
    por_rep, sem_rep = cartoes_da_fatura(comp)
    ativas = {i.representante_id: i for i in Importacao.objects.filter(
        tipo=Importacao.Tipo.PLANILHA, competencia=comp, ativo=True)}
    reps = {r.pk: r for r in Representante.objects.filter(pk__in=set(por_rep) | set(ativas))}
    linhas = [{"rep": reps[rid], "cartoes": por_rep.get(rid, []), "planilha": ativas.get(rid),
               "concilia": any(c.escopo == Cartao.Escopo.CONCILIAR_FORMULARIO for c in por_rep.get(rid, []))}
              for rid in sorted(reps, key=lambda i: reps[i].nome)]
    historico = (Importacao.objects.filter(tipo=Importacao.Tipo.PLANILHA, competencia=comp)
                 .select_related("representante").order_by("-criado_em")[:50])
    return render(request, "conciliacao/planilhas.html", {"competencias": competencias, "comp": comp,
                                                          "linhas": linhas, "historico": historico,
                                                          "sem_representante": sem_rep,
                                                          "para_geral": competencias_para_geral(comp),
                                                          "seletor": seletor_competencias(competencias, comp)})


def cartoes_da_fatura(comp):
    """Cartões que aparecem na fatura em uso da competência: ({representante_id: [cartões]}, [cartões sem
    representante]). As telas de planilhas e o painel mostram só esses (cada banco, os seus)."""
    fatura = comp.importacoes.filter(tipo=Importacao.Tipo.FATURA, ativo=True).first()
    if fatura is None:
        return {}, []
    cartoes = Cartao.objects.filter(lancamentos__importacao=fatura).distinct().select_related("representante")
    por_rep, sem_rep = defaultdict(list), []
    for c in cartoes.order_by("final"):
        if c.representante_id:
            por_rep[c.representante_id].append(c)
        else:
            sem_rep.append(c)
    return dict(por_rep), sem_rep


MESES_NOME = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
              "novembro", "dezembro"]


def seletor_competencias(competencias, atual):
    """Dados do seletor banco → ano → mês: {slug: {"nome":…, "anos": {ano: [[mês, código, rótulo], …]}}}."""
    arvore = {}
    for c in sorted(competencias, key=lambda c: (c.conta.nome, c.ano_mes)):
        conta = arvore.setdefault(c.conta.slug, {"nome": c.conta.nome, "anos": {}})
        ano, mes = c.ano_mes.split("-")
        rotulo = MESES_NOME[int(mes) - 1] + (" (fechada)" if c.fechada else "")
        conta["anos"].setdefault(ano, []).append([mes, c.codigo, rotulo])
    return {"arvore": arvore, "conta": atual.conta.slug, "ano": atual.ano_mes[:4], "codigo": atual.codigo}


def competencias_para_geral(comp):
    """Faturas do mesmo banco, em aberto e com fatura em uso: onde a planilha geral pode entrar."""
    return list(Competencia.objects.filter(conta_id=comp.conta_id, status=Competencia.Status.ABERTA,
                                           importacoes__tipo=Importacao.Tipo.FATURA, importacoes__ativo=True)
                .distinct().order_by("periodo_ini"))


@require_POST
def planilha_geral_enviar(request, codigo):
    """Planilha GERAL REPRESENTANTES: entra nas faturas marcadas, em segundo plano."""
    comp = _competencia(codigo)
    arquivo = request.FILES.get("arquivo")
    validas = {c.pk: c for c in competencias_para_geral(comp)}
    escolhidas = [validas[int(i)] for i in request.POST.getlist("competencias") if i.isdigit() and int(i) in validas]
    if not arquivo or not escolhidas:
        messages.error(request, "Escolha a planilha geral e pelo menos uma fatura.")
        return redirect("conciliacao:planilhas_competencia", codigo=comp.codigo)
    try:
        _, caminho = arquivos.guardar(arquivo, arquivo.name, subpasta="planilhas", extensoes={".xlsx"},
                                      assinatura=planilha_servico.ASSINATURA_XLSX, descricao="uma planilha .xlsx")
    except ArquivoRecusado as exc:
        messages.error(request, str(exc))
        return redirect("conciliacao:planilhas_competencia", codigo=comp.codigo)
    tarefas.enfileirar_geral(str(caminho), arquivo.name, [c.pk for c in escolhidas], _usuario(request),
                             request.ip_cliente)
    messages.success(request, f"Planilha geral recebida para {len(escolhidas)} fatura(s). Ela está sendo distribuída "
                              f"por representante e conciliada em segundo plano: atualize a página em 1 ou 2 minutos.")
    return redirect("conciliacao:planilhas_competencia", codigo=comp.codigo)


@require_POST
def planilha_enviar(request, codigo, rep_id):
    comp = _competencia(codigo)
    rep = get_object_or_404(Representante, pk=rep_id, ativo=True)
    voltar = redirect("conciliacao:planilhas_competencia", codigo=comp.codigo)
    arquivo = request.FILES.get("arquivo")
    if not arquivo:
        messages.error(request, f"Escolha a planilha do(a) {rep.nome}.")
        return voltar
    try:
        imp, criada = planilha_servico.criar_importacao(arquivo, arquivo.name, rep, comp, usuario=_usuario(request),
                                                        ip=request.ip_cliente, maquina=nome_maquina(request.ip_cliente))
    except ArquivoRecusado as exc:
        messages.error(request, f"{rep.nome}: {exc}")
        return voltar
    if not criada:
        messages.info(request, f"{rep.nome}: este arquivo é o mesmo da planilha em uso (v{imp.versao}). Nada foi alterado.")
    else:
        tarefas.enfileirar_planilha(imp.pk)
    return redirect("conciliacao:planilha_detalhe", pk=imp.pk)


def _ctx_planilha(imp):
    return {"imp": imp, "r": imp.resumo or {},
            "competencias": Competencia.objects.filter(status=Competencia.Status.ABERTA).order_by("-periodo_ini")}


def planilha_detalhe(request, pk):
    imp = get_object_or_404(Importacao.objects.select_related("representante"), pk=pk, tipo=Importacao.Tipo.PLANILHA)
    return render(request, "conciliacao/planilha_detalhe.html", _ctx_planilha(imp))


def planilha_parcial(request, pk):
    imp = get_object_or_404(Importacao.objects.select_related("representante"), pk=pk, tipo=Importacao.Tipo.PLANILHA)
    return render(request, "conciliacao/_planilha_resultado.html", _ctx_planilha(imp))


# ---------- painel da competência ----------

def _somar(qs):
    linha = qs.aggregate(v=Sum("valor_trava"), n=Count("id"))
    return linha["v"] or ZERO, linha["n"]


def painel(request, codigo=None):
    competencias = list(Competencia.objects.select_related("conta").order_by("-periodo_ini", "conta__nome"))
    if not competencias:
        return render(request, "conciliacao/painel.html", {"competencias": []})
    comp = _competencia(codigo) if codigo else competencias[0]
    vigentes = Resultado.objects.filter(competencia=comp, vigente=True)
    total = comp.total_fatura
    grupos = []
    for chave, rotulo, classe, status in GRUPOS:
        valor, qtd = _somar(vigentes.filter(status__in=status, fatura_lanc__isnull=False))
        grupos.append({"chave": chave, "rotulo": rotulo, "classe": classe, "valor": valor, "qtd": qtd,
                       "pct": (valor / total * 100) if total else ZERO})
    soma = sum((g["valor"] for g in grupos), ZERO)
    execucao = comp.execucoes.order_by("-inicio").first()

    # Tabela por representante
    por_rep = defaultdict(lambda: defaultdict(lambda: [ZERO, 0]))
    for linha in (vigentes.exclude(representante=None).values("representante", "status")
                  .annotate(v=Sum("valor_trava"), n=Count("id"))):
        por_rep[linha["representante"]][linha["status"]] = [linha["v"] or ZERO, linha["n"]]
    reps = {r.pk: r for r in Representante.objects.filter(pk__in=por_rep)}
    # Só os cartões que aparecem nesta fatura (cada banco mostra os seus).
    cartoes_rep = defaultdict(set)
    for rep_id, final in vigentes.exclude(representante=None).exclude(cartao=None).values_list(
            "representante", "cartao__final").distinct():
        cartoes_rep[rep_id].add(final)
    tabela = []
    for rep_id, st in por_rep.items():
        rep = reps[rep_id]
        fatura = sum((v for v, n in st.values()), ZERO)   # linhas só do formulário não têm valor da fatura
        conciliado = st[S.CONCILIADO][0] + st[S.CONCILIADO_RESSALVA][0]
        if st[S.AGUARDANDO_PLANILHA][1]:
            situacao, classe = "aguardando planilha", "st-neutro"
        elif st[S.FORA_ESCOPO][1] and not (st[S.CONCILIADO][1] or st[S.SEM_FORMULARIO][1] or st[S.SEM_NOTA][1]):
            situacao, classe = "fora do escopo", "st-neutro"
        elif st[S.SEM_FORMULARIO][1] or st[S.SEM_NOTA][1] or st[S.SEM_COBRANCA][1] or st[S.PENDENTE_ANALISE][1]:
            situacao, classe = "com pendências", "st-semform"
        elif st[S.AGUARDANDO_APROVACAO][1]:
            situacao, classe = "aguardando aprovação", "st-laranja"
        elif st[S.CONCILIADO_RESSALVA][1]:
            situacao, classe = "conciliado com ressalva", "st-ressalva"
        else:
            situacao, classe = "conciliado", "st-ok"
        tabela.append({"rep": rep, "cartoes": ", ".join(sorted(cartoes_rep[rep_id])), "fatura": fatura,
                       "conciliado": conciliado, "sem_formulario": st[S.SEM_FORMULARIO][0] + st[S.SEM_NOTA][0],
                       "sem_formulario_n": st[S.SEM_FORMULARIO][1] + st[S.SEM_NOTA][1], "sem_cobranca_n": st[S.SEM_COBRANCA][1],
                       "analise_n": st[S.PENDENTE_ANALISE][1],
                       "pct": (conciliado / fatura * 100) if fatura else None, "situacao": situacao, "classe": classe})
    filtro = request.GET.get("situacao", "")
    if filtro:
        tabela = [t for t in tabela if t["situacao"] == filtro]
    tabela.sort(key=lambda t: t["rep"].nome)

    alertas = {
        "datas_corrigidas": vigentes.filter(form_despesa__data_suspeita=True).count(),
        "duplicados": vigentes.filter(status=S.DUPLICADO_FORM).count(),
        "reembolsos": vigentes.filter(status=S.REEMBOLSO).count(),
        "analise": vigentes.filter(status=S.PENDENTE_ANALISE).count(),
        "sem_cobranca": vigentes.filter(status=S.SEM_COBRANCA).count(),
        "secoes_com_erro": comp.importacoes.filter(tipo=Importacao.Tipo.FATURA, ativo=True,
                                                   secoes__status="ERRO_LEITURA").count(),
    }
    from .models import Cartao
    alertas["cartoes_pendentes"] = Cartao.objects.filter(pendente_cadastro=True, conta=comp.conta).count()
    fatura = comp.importacoes.filter(tipo=Importacao.Tipo.FATURA, ativo=True).first()
    # avisos de conferência (só leitura): edições que se soltaram ao trocar a versão e compras repetidas
    soltas = avisos.edicoes_soltas(comp, fatura)
    alertas["edicoes_soltas"] = [(tipo, avisos.descrever(chave)) for tipo, chave in soltas]
    alertas["repetidas"] = len(avisos.cobrancas_repetidas(fatura))
    por_rep_fatura, sem_rep = cartoes_da_fatura(comp)
    precisam = {rid for rid, cs in por_rep_fatura.items() if any(c.escopo == Cartao.Escopo.CONCILIAR_FORMULARIO for c in cs)}
    com_planilha = set(Importacao.objects.filter(tipo=Importacao.Tipo.PLANILHA, ativo=True, competencia=comp)
                       .values_list("representante_id", flat=True))
    return render(request, "conciliacao/painel.html", {
        "competencias": competencias, "comp": comp, "grupos": grupos, "soma": soma, "diferenca": soma - total,
        "execucao": execucao, "tabela": tabela, "alertas": alertas, "fatura": fatura, "filtro": filtro,
        "ultimo_backup": Backup.objects.filter(tipo=Backup.Tipo.BACKUP).first(),
        "ultimo_teste": Backup.objects.filter(tipo=Backup.Tipo.TESTE_RESTAURACAO).first(),
        "situacoes": ["com pendências", "aguardando aprovação", "conciliado com ressalva", "conciliado", "aguardando planilha",
                      "fora do escopo"],
        "planilhas": (len([r for r in precisam if r in com_planilha]), len(precisam)),
        "sem_representante": sem_rep,
        "seletor": seletor_competencias(competencias, comp),
        "notas_pendentes": (vigentes.filter(status=S.SEM_NOTA).count()
                            if any(c.escopo == Cartao.Escopo.FASE2_NF for cs in por_rep_fatura.values() for c in cs)
                            else None),
    })


# ---------- detalhe do representante ----------

def _soma_reembolso(resultados):
    """Reembolso do formulário, com o valor corrigido pelo financeiro quando houver (o mesmo do dashboard)."""
    corrigidos = dict(CorrecaoValor.objects.filter(ativo=True).values_list("chave", "valor_corrigido"))
    total = ZERO
    for r in resultados.filter(status=S.REEMBOLSO, form_despesa__isnull=False).select_related("form_despesa"):
        valor = corrigidos.get(chave_despesa(r.form_despesa), r.form_despesa.valor)
        total += valor or ZERO
    return total


def representante(request, codigo, rep_id):
    comp = _competencia(codigo)
    rep = get_object_or_404(Representante, pk=rep_id)
    todos = (Resultado.objects.filter(competencia=comp, vigente=True, representante=rep)
             .select_related("fatura_lanc__cartao", "form_despesa__importacao", "cartao", "ajuste"))
    contagem = {linha["status"]: linha["n"] for linha in todos.values("status").annotate(n=Count("id"))}
    filtro = request.GET.get("status", "")
    # Linha que acabou de ser ajustada: continua na tela (destacada) mesmo que tenha saído do filtro.
    destaque = request.GET.get("linha", "")
    alvo = Q()
    if destaque[:1] in ("L", "F") and destaque[1:].isdigit():
        alvo = Q(fatura_lanc_id=int(destaque[1:])) if destaque[0] == "L" else Q(form_despesa_id=int(destaque[1:]))
    linhas = todos.filter(Q(status=filtro) | alvo) if filtro and alvo else (todos.filter(status=filtro) if filtro else todos)
    linhas = sorted(linhas, key=lambda r: (r.fatura_lanc.data if r.fatura_lanc else
                                           (r.form_despesa.data_efetiva or r.form_despesa.carimbo.date()),
                                           r.fatura_lanc.ordem if r.fatura_lanc else 0, r.pk))

    def soma(*status):
        return todos.filter(status__in=status, fatura_lanc__isnull=False).aggregate(v=Sum("valor_trava"))["v"] or ZERO

    indicadores = [
        ("Na fatura", todos.filter(fatura_lanc__isnull=False).aggregate(v=Sum("valor_trava"))["v"] or ZERO, ""),
        ("Conciliado", soma(S.CONCILIADO, S.CONCILIADO_RESSALVA), "st-ok"),
        ("Com ressalva", soma(S.CONCILIADO_RESSALVA), "st-ressalva"),
        ("Sem justificativa", soma(S.SEM_FORMULARIO), "st-semform"),
        # pago pelo representante: não está na fatura, fica à parte (mesmo valor do dashboard, com a correção)
        ("Reembolsado (fora da fatura)", _soma_reembolso(todos), "st-info"),
    ]
    filtros = [(s, rotulo_situacao(s), contagem[s], CLASSE_STATUS[s]) for s in Resultado.Status.values
               if s in contagem]
    anexos = defaultdict(list)
    for a in Anexo.objects.filter(ativo=True).order_by("criado_em"):
        anexos[a.chave].append(a)
    editavel = not comp.fechada and _representante_concilia(rep)
    correcoes = {c.chave: c for c in CorrecaoValor.objects.filter(representante=rep, ativo=True)}
    outras = {(c.chave, c.campo): c for c in CorrecaoDespesa.objects.filter(representante=rep, ativo=True)}
    # Portal de pendências: respostas do representante e links já gerados para esta fatura
    respostas = {x.chave: x for x in RespostaPendencia.objects.filter(ativo=True, link__representante=rep,
                                                                       link__competencia=comp)}
    links = list(LinkPendencias.objects.filter(representante=rep, competencia=comp).order_by("-criado_em")[:10])
    for lk in links:
        lk.respondidos = sum(1 for c in lk.chaves if c in respostas)
    finalidades = {f.chave: f for f in FinalidadeLancamento.objects.filter(ativo=True)
                   .select_related("evento")}
    fatura_comp = comp.importacoes.filter(tipo=Importacao.Tipo.FATURA, ativo=True).first()
    repetidas = avisos.cobrancas_repetidas(fatura_comp)
    estornos = avisos.estornos_provaveis([r.fatura_lanc for r in linhas if r.fatura_lanc_id and r.fatura_lanc.valor < 0
                                          and r.status == S.PENDENTE_ANALISE])
    for r in linhas:
        r.resposta = respostas.get(chave_lancamento(r.fatura_lanc)) if r.fatura_lanc_id else None
        r.cor = COR_LINHA.get(r.status, "linha-neutra")
        r.ref = f"L{r.fatura_lanc_id}" if r.fatura_lanc_id else f"F{r.form_despesa_id}"
        r.destacada = r.ref == destaque
        r.chave = chave_resultado(r)
        r.anexos = anexos.get(r.chave, []) + (anexos.get(chave_despesa(r.form_despesa), [])
                                              if r.fatura_lanc and r.form_despesa else [])
        r.opcoes = _opcoes(r) if editavel and _editavel(r) else []
        r.correcao = correcoes.get(chave_despesa(r.form_despesa)) if r.form_despesa else None
        if r.form_despesa:
            chave = chave_despesa(r.form_despesa)
            r.corr_data = outras.get((chave, CorrecaoDespesa.Campo.DATA))
            r.corr_categoria = outras.get((chave, CorrecaoDespesa.Campo.CATEGORIA))
            r.data_form = (date.fromisoformat(r.corr_data.novo) if r.corr_data
                           else r.form_despesa.data_corrigida or r.form_despesa.data_informada)
            r.categoria = (r.corr_categoria.novo if r.corr_categoria
                           else parametros.categoria_da_lista(r.form_despesa.despesa))
        elif r.fatura_lanc:
            r.corr_categoria = outras.get((chave_lancamento(r.fatura_lanc), CorrecaoDespesa.Campo.CATEGORIA))
            r.categoria = r.corr_categoria.novo if r.corr_categoria else ""
        declarado = r.correcao.valor_corrigido if r.correcao else (r.form_despesa.valor if r.form_despesa else None)
        r.diferenca_valor = (r.fatura_lanc is not None and declarado is not None and declarado != r.fatura_lanc.valor)
        # A fatura vale; a diferença aparece sempre e é "Divergente" quando não se explica por parcelamento
        # (formulário com o total, fatura com a parcela) nem por conta dividida (vários formulários num lançamento)
        r.diferenca = declarado - r.fatura_lanc.valor if r.diferenca_valor else None
        r.divergente = r.diferenca_valor and r.regra not in (Resultado.Regra.PARCELADO, Resultado.Regra.DIVIDIDA)
        r.escolha = finalidades.get(r.chave)
        r.repetida = repetidas.get(r.fatura_lanc_id) if r.fatura_lanc_id else None
        r.estorno_de = estornos.get(r.fatura_lanc_id) if r.fatura_lanc_id else None
    return render(request, "conciliacao/representante.html", {
        "comp": comp, "rep": rep, "linhas": linhas, "indicadores": indicadores, "filtros": filtros,
        "filtro": filtro, "total": len(todos), "classes": CLASSE_STATUS, "editavel": editavel,
        "cores": COR_LINHA, "exigem_motivo": EXIGEM_MOTIVO, "categorias": list(parametros.CATEGORIAS_DESPESA),
        "links": links, "validades": pendencias.VALIDADES, "finalidades": FinalidadeLancamento.Finalidade.choices,
        "eventos": list(eventos.ativos()),
        "pendentes_portal": pendencias.lancamentos_pendentes(rep, comp).count() if editavel else 0,
        "voltar": request.get_full_path()})


# ---------- ajustes manuais na linha ----------

# Linha inteira: verde = conciliado; amarelo = precisa de atenção; vermelho = falta informação.
COR_LINHA = {
    S.CONCILIADO: "linha-verde",
    S.CONCILIADO_RESSALVA: "linha-amarela", S.PENDENTE_ANALISE: "linha-amarela", S.SEM_COBRANCA: "linha-amarela",
    S.SEM_FORMULARIO: "linha-vermelha", S.AGUARDANDO_PLANILHA: "linha-vermelha", S.SEM_NOTA: "linha-vermelha",
    S.AGUARDANDO_APROVACAO: "linha-laranja",
}
# Situações que uma pessoa pode escolher, conforme o lado da linha. A trava não muda: o valor da fatura continua
# somando no total; muda só o grupo em que aparece.
# "Reembolso" em todas as listas (pedido de 22/10/2026): na fatura, gasto que o representante devolve/é reembolsado.
OPCOES_FATURA = [S.CONCILIADO, S.CONCILIADO_RESSALVA, S.SEM_FORMULARIO, S.PENDENTE_ANALISE, S.REEMBOLSO,
                 S.ESTORNO_COMPENSADO, S.FORA_ESCOPO]
OPCOES_NOTA = [S.CONCILIADO, S.CONCILIADO_RESSALVA, S.SEM_NOTA, S.PENDENTE_ANALISE, S.REEMBOLSO, S.FORA_ESCOPO]
OPCOES_FORMULARIO = [S.CONCILIADO, S.CONCILIADO_RESSALVA, S.SEM_COBRANCA, S.PENDENTE_ANALISE, S.REEMBOLSO,
                     S.DUPLICADO_FORM, S.FORA_ESCOPO]
# Escolhas que exigem o motivo escrito (aparece na linha e na auditoria).
EXIGEM_MOTIVO = {S.CONCILIADO_RESSALVA}
TIPOS_ANEXO = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp"}
ASSINATURAS_ANEXO = (b"%PDF-", b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF")


ESCOPOS_AJUSTAVEIS = [Cartao.Escopo.CONCILIAR_FORMULARIO, Cartao.Escopo.FASE2_NF]


def _representante_concilia(rep):
    return rep.cartoes.filter(escopo__in=ESCOPOS_AJUSTAVEIS).exists()


def _editavel(r):
    if r.fatura_lanc is not None:
        return r.fatura_lanc.secao == FaturaLanc.Secao.PORTADOR and (
            r.cartao is None or r.cartao.escopo in ESCOPOS_AJUSTAVEIS)
    return True


def _opcoes(r):
    if r.fatura_lanc is None:
        opcoes = OPCOES_FORMULARIO
    elif r.cartao is not None and r.cartao.escopo == Cartao.Escopo.FASE2_NF:
        opcoes = OPCOES_NOTA
    else:
        opcoes = OPCOES_FATURA
    return [(s, rotulo_situacao(s)) for s in opcoes]


def _voltar(request, comp, rep, r=None):
    """Volta para a tela de origem; com r, a linha alterada fica visível e destacada (mesmo fora do filtro)."""
    destino = request.POST.get("voltar", "")
    if not (destino and url_has_allowed_host_and_scheme(destino, allowed_hosts={request.get_host()})
            and destino.startswith("/")):
        destino = reverse("conciliacao:representante", kwargs={"codigo": comp.codigo, "rep_id": rep.pk})
    if r is None:
        return redirect(destino)
    ref = f"L{r.fatura_lanc_id}" if r.fatura_lanc_id else f"F{r.form_despesa_id}"
    partes = urlsplit(destino)
    consulta = [(k, v) for k, v in parse_qsl(partes.query) if k != "linha"] + [("linha", ref)]
    # sem "#r-…": a própria página volta para a posição em que a pessoa estava (rolagem guardada no envio)
    return redirect(urlunsplit((partes.scheme, partes.netloc, partes.path, urlencode(consulta), "")))


def _resultado_editavel(pk):
    r = get_object_or_404(Resultado.objects.select_related("competencia", "representante", "cartao", "fatura_lanc__cartao",
                                                           "form_despesa"), pk=pk, vigente=True)
    if r.competencia.fechada:
        raise PermissionDenied("Competência fechada.")
    if r.representante is None or not _representante_concilia(r.representante) or not _editavel(r):
        raise PermissionDenied("Esta linha não é ajustável.")
    return r


def _reconciliar(r, usuario):
    cartoes = list(Cartao.objects.filter(representante=r.representante, escopo__in=ESCOPOS_AJUSTAVEIS))
    motor.executar(r.competencia, cartoes, motivo="ajuste manual", usuario=usuario)


@require_POST
def alterar_situacao(request, pk):
    r = _resultado_editavel(pk)
    novo = request.POST.get("status", "")
    observacao = request.POST.get("observacao", "").strip()[:500]
    chave = chave_resultado(r)
    permitidos = {s for s, _ in _opcoes(r)}
    if novo != "AUTOMATICO" and novo not in permitidos:
        messages.error(request, "Situação inválida para esta linha.")
        return _voltar(request, r.competencia, r.representante)
    if novo in EXIGEM_MOTIVO and len(observacao) < 3:
        messages.error(request, f"Para “{rotulo_situacao(novo)}”, escreva o motivo da ressalva.")
        return _voltar(request, r.competencia, r.representante)
    usuario = _usuario(request)
    with transaction.atomic():
        AjusteManual.objects.filter(competencia=r.competencia, chave=chave, ativo=True).update(ativo=False)
        if novo != "AUTOMATICO":
            AjusteManual.objects.create(competencia=r.competencia, chave=chave, status=novo, observacao=observacao,
                                        usuario=usuario, ip=request.ip_cliente)
    auditoria.registrar(request, "AJUSTE_MANUAL", "OK", competencia=r.competencia, representante=r.representante,
                        cartao=r.cartao, detalhe={"resultado": r.pk, "de": r.status, "para": novo,
                                                  "status_automatico": r.status_automatico})
    _reconciliar(r, usuario)
    messages.success(request, "Situação voltou para a automática." if novo == "AUTOMATICO"
                     else f"Situação alterada para {rotulo_situacao(novo)}.")
    return _voltar(request, r.competencia, r.representante, r)


def _ler_valor_digitado(texto: str):
    """'1.234,56', '1234,56', '1234.56' ou 'R$ 45' -> Decimal; outra coisa -> None."""
    t = (texto or "").strip().replace("R$", "").replace(" ", "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        valor = Decimal(t).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return valor if 0 < valor < Decimal("1000000") else None


@require_POST
def corrigir_valor(request, pk):
    """Corrige o valor que o representante informou (nunca o da fatura). O original fica guardado; a conciliação
    é refeita com o valor corrigido. Valor vazio + "desfazer" volta ao informado."""
    r = _resultado_editavel(pk)
    if r.form_despesa is None:
        raise PermissionDenied("Só o valor informado pelo representante pode ser corrigido.")
    d = r.form_despesa
    chave = chave_despesa(d)
    usuario = _usuario(request)
    desfazer = request.POST.get("acao") == "desfazer"
    motivo = request.POST.get("motivo", "").strip()[:500]
    valor = None if desfazer else _ler_valor_digitado(request.POST.get("valor", ""))
    if not desfazer:
        if valor is None:
            messages.error(request, "Valor inválido. Use, por exemplo, 1.234,56.")
            return _voltar(request, r.competencia, r.representante)
        if len(motivo) < 3:
            messages.error(request, "Escreva o motivo da correção (ex.: conferido no recibo).")
            return _voltar(request, r.competencia, r.representante)
        # Trava: mudar o valor de um gasto que já fecha outra cobrança faz ele contar duas vezes (caso real:
        # formulário de conta dividida em maio corrigido para casar com a parcela de outubro).
        ja_usado = (Resultado.objects.filter(vigente=True, form_despesa=d, fatura_lanc__isnull=False,
                                             status__in=[S.CONCILIADO, S.CONCILIADO_RESSALVA])
                    .filter(Q(regra=Resultado.Regra.DIVIDIDA) | ~Q(competencia=r.competencia))
                    .select_related("competencia__conta", "fatura_lanc").first())
        if ja_usado is not None:
            onde = (f"na conta dividida da fatura {ja_usado.competencia}" if ja_usado.regra == Resultado.Regra.DIVIDIDA
                    else f"na fatura {ja_usado.competencia}")
            messages.error(request, f"Este gasto já está conciliado {onde} (lançamento de "
                                    f"{ja_usado.fatura_lanc.data:%d/%m/%Y}, R$ {ja_usado.fatura_lanc.valor}). "
                                    "Corrigir o valor faria ele contar duas vezes. Para uma cobrança sem formulário, "
                                    "mude a situação da linha da fatura (ou peça ao representante pelo link).")
            return _voltar(request, r.competencia, r.representante)
    with transaction.atomic():
        anterior = CorrecaoValor.objects.filter(chave=chave, ativo=True).first()
        CorrecaoValor.objects.filter(chave=chave, ativo=True).update(ativo=False)
        if not desfazer:
            CorrecaoValor.objects.create(chave=chave, representante=d.representante, valor_original=d.valor,
                                         valor_original_texto=d.valor_original[:200], valor_corrigido=valor,
                                         motivo=motivo, usuario=usuario, ip=request.ip_cliente)
    auditoria.registrar(request, "AJUSTE_MANUAL", "OK", competencia=r.competencia, representante=r.representante,
                        cartao=r.cartao, detalhe={"correcao_valor": True, "resultado": r.pk,
                                                  "original": str(d.valor) if d.valor is not None else d.valor_original,
                                                  "antes": str(anterior.valor_corrigido) if anterior else None,
                                                  "depois": str(valor) if valor is not None else None})
    _reconciliar(r, usuario)
    messages.success(request, "Valor voltou ao informado pelo representante." if desfazer
                     else f"Valor corrigido para R$ {valor:.2f}".replace(".", ",") + ". A conciliação foi refeita.")
    return _voltar(request, r.competencia, r.representante, r)


@require_POST
def escolher_finalidade(request, pk):
    """Finalidade (PROCEDIMENTO / NEGOCIAÇÃO / EVENTO) e Evento da linha, preenchidos pelo financeiro. Com a opção
    "+ Cadastrar novo evento", o nome digitado vira evento novo (normalizado, sem duplicar)."""
    r = _resultado_editavel(pk)
    try:
        eventos.escolher(r, chave_resultado(r), request.POST.get("finalidade", ""), request.POST.get("evento", ""),
                         request.POST.get("novo_evento", ""), usuario=_usuario(request), ip=request.ip_cliente)
    except eventos.EscolhaInvalida as exc:
        messages.error(request, str(exc))
    return _voltar(request, r.competencia, r.representante, r)


@require_POST
def corrigir_despesa(request, pk):
    """Corrige a data ou a categoria que o representante preencheu (o original fica). "desfazer" volta ao informado.
    Categoria: uma das opções da lista. Data: a data certa do gasto."""
    r = _resultado_editavel(pk)
    d = r.form_despesa
    campo = request.POST.get("campo", "")
    if campo not in CorrecaoDespesa.Campo.values:
        raise PermissionDenied("Campo inválido.")
    if d is None and campo != CorrecaoDespesa.Campo.CATEGORIA:
        raise PermissionDenied("Sem formulário, só a categoria pode ser informada.")
    # Sem formulário (só na fatura): a categoria fica presa ao lançamento ("Não informado" até alguém escolher).
    chave = chave_despesa(d) if d is not None else chave_lancamento(r.fatura_lanc)
    usuario = _usuario(request)
    motivo = request.POST.get("motivo", "").strip()[:500]
    desfazer = request.POST.get("acao") == "desfazer"
    bruto = request.POST.get("valor", "").strip()
    if campo == CorrecaoDespesa.Campo.CATEGORIA:
        original = d.despesa if d is not None else ""
        novo = bruto if bruto in parametros.CATEGORIAS_DESPESA else None
        # escolher de novo exatamente o que foi informado (ou "Não informado") = voltar ao informado
        desfazer = desfazer or (novo is not None and d is not None and novo == parametros.categoria_da_lista(d.despesa))
        desfazer = desfazer or (d is None and not bruto)
    else:
        original = (d.data_informada or d.data_corrigida).isoformat() if (d.data_informada or d.data_corrigida) else ""
        try:
            novo = date.fromisoformat(bruto).isoformat()
        except ValueError:
            novo = None
        if novo and not 2000 <= int(novo[:4]) <= date.today().year + 1:
            novo = None
    if not desfazer and novo is None:
        messages.error(request, "Escolha uma categoria da lista." if campo == CorrecaoDespesa.Campo.CATEGORIA
                       else "Data inválida.")
        return _voltar(request, r.competencia, r.representante)
    with transaction.atomic():
        anterior = CorrecaoDespesa.objects.filter(chave=chave, campo=campo, ativo=True).first()
        CorrecaoDespesa.objects.filter(chave=chave, campo=campo, ativo=True).update(ativo=False)
        if not desfazer:
            CorrecaoDespesa.objects.create(chave=chave, representante=r.representante, campo=campo,
                                           original=original[:200], novo=novo, motivo=motivo, usuario=usuario,
                                           ip=request.ip_cliente)
    auditoria.registrar(request, "AJUSTE_MANUAL", "OK", competencia=r.competencia, representante=r.representante,
                        cartao=r.cartao, detalhe={"correcao": campo, "resultado": r.pk, "original": original,
                                                  "antes": anterior.novo if anterior else None,
                                                  "depois": None if desfazer else novo})
    _reconciliar(r, usuario)
    nome = "Categoria" if campo == CorrecaoDespesa.Campo.CATEGORIA else "Data"
    messages.success(request, f"{nome} voltou ao informado pelo representante." if desfazer
                     else f"{nome} corrigida. A conciliação foi refeita.")
    return _voltar(request, r.competencia, r.representante, r)


@require_POST
def anexar_comprovante(request, pk):
    r = _resultado_editavel(pk)
    arquivo = request.FILES.get("arquivo")
    if not arquivo:
        messages.error(request, "Escolha o arquivo do comprovante.")
        return _voltar(request, r.competencia, r.representante)
    extensao = Path(arquivo.name).suffix.lower()
    try:
        hash_arquivo, caminho = arquivos.guardar(arquivo, arquivo.name, subpasta="comprovantes",
                                                 extensoes=set(TIPOS_ANEXO), assinatura=ASSINATURAS_ANEXO,
                                                 descricao="um PDF ou imagem (JPG, PNG, WEBP)")
    except ArquivoRecusado as exc:
        messages.error(request, str(exc))
        return _voltar(request, r.competencia, r.representante)
    # O comprovante pertence à despesa; sem resposta no formulário, ao lançamento da fatura.
    chave = chave_despesa(r.form_despesa) if r.form_despesa else chave_lancamento(r.fatura_lanc)
    usuario = _usuario(request)
    anexo = Anexo.objects.create(competencia=r.competencia, chave=chave, arquivo_caminho=str(caminho),
                                 nome_original=arquivo.name[:255], mime=TIPOS_ANEXO[extensao],
                                 hash_sha256=hash_arquivo, tamanho=arquivo.size, usuario=usuario,
                                 ip=request.ip_cliente)
    auditoria.registrar(request, "ANEXO", "OK", competencia=r.competencia, representante=r.representante,
                        cartao=r.cartao, detalhe={"anexo": anexo.pk, "resultado": r.pk, "hash": hash_arquivo})
    _reconciliar(r, usuario)
    messages.success(request, f"Comprovante {arquivo.name} anexado.")
    return _voltar(request, r.competencia, r.representante, r)


@require_POST
def remover_anexo(request, pk):
    anexo = get_object_or_404(Anexo, pk=pk, ativo=True)
    if anexo.competencia.fechada:
        raise PermissionDenied("Competência fechada.")
    anexo.ativo = False
    anexo.save(update_fields=["ativo"])
    auditoria.registrar(request, "ANEXO", "OK", competencia=anexo.competencia,
                        detalhe={"anexo": anexo.pk, "removido": True})
    resultado = (Resultado.objects.filter(competencia=anexo.competencia, vigente=True)
                 .select_related("representante", "competencia", "fatura_lanc__cartao", "form_despesa")
                 .filter(Q(form_despesa__isnull=False) | Q(fatura_lanc__isnull=False)))
    alvo = next((r for r in resultado if r.representante and (
        chave_resultado(r) == anexo.chave or (r.form_despesa and chave_despesa(r.form_despesa) == anexo.chave))), None)
    if alvo:
        _reconciliar(alvo, _usuario(request))
        messages.success(request, f"Comprovante {anexo.nome_original} removido.")
        return _voltar(request, alvo.competencia, alvo.representante)
    messages.success(request, f"Comprovante {anexo.nome_original} removido.")
    return redirect("conciliacao:painel_competencia", codigo=anexo.competencia.codigo)


def ver_anexo(request, pk):
    """Entrega o arquivo pela aplicação (com o mesmo controle de acesso), nunca por URL estática."""
    anexo = get_object_or_404(Anexo, pk=pk)
    caminho = Path(anexo.arquivo_caminho)
    if not caminho.is_file():
        raise Http404("Arquivo não encontrado.")
    # Só PDF/JPG/PNG/WEBP com assinatura conferida no envio; o tipo vem do cadastro do anexo (nosniff já é global).
    return FileResponse(open(caminho, "rb"), content_type=anexo.mime, as_attachment=False,
                        filename=anexo.nome_original)
