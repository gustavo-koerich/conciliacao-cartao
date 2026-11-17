"""Análise de gastos. A FATURA é a fonte da verdade (26/10/2026): os totais usam o valor faturado; formulário,
resposta pelo link e escolhas do financeiro só complementam (categoria, médico, paciente, finalidade, evento).
Reembolso aparece à parte (fora da fatura) e formulário sem lançamento não entra no total. Ver coletar().
A base geral (planilha GERAL REPRESENTANTES) cobre só os períodos sem planilha no sistema.

A lista geral sai no mesmo layout da GERAL (para completar à mão no Excel e reimportar como nova versão da base).
"""
import datetime as dt
import io
import logging
from decimal import Decimal, InvalidOperation

import openpyxl
from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from . import arquivos, auditoria, parametros

from .models import (CorrecaoDespesa, CorrecaoValor, GastoBase, Importacao, Representante,
                     Resultado, chave_despesa)
from .parametros import normalizar_texto

log = logging.getLogger(__name__)

ASSINATURA_XLSX = b"PK\x03\x04"
COLUNAS_OBRIGATORIAS = ("Nome da Origem", "Data", "Despesa", "Valor")
MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto", "Setembro", "Outubro",
         "Novembro", "Dezembro"]
# Layout da GERAL na exportação, mais as colunas de controle do sistema no fim.
COLUNAS_EXPORTACAO = ["Nome da Origem", "Mês", "Data", "Despesa", "Descrição", "Finalidade", "Pagamento", "Médico",
                      "Paciente", "Cidade", "Valor", "Ano", "Categoria (análise)", "Fonte", "Situação na conciliação",
                      "Mês da fatura", "Evento", "Valor informado", "Diferença", "Divergente", "Entra no total da fatura"]


class BaseInvalida(Exception):
    pass


def categoria(despesa: str, de_para: dict) -> str:
    da_lista = parametros.categoria_da_lista(despesa)        # as 19 categorias da tela do representante
    if da_lista:
        return parametros.CATEGORIAS_DESPESA[da_lista]
    return de_para.get(normalizar_texto(despesa), "Outros")


def _texto(v) -> str:
    return " ".join(str(v).replace("\xa0", " ").split()) if v is not None else ""


def _titulo(nome: str) -> str:
    nome = _texto(nome)
    return " ".join(p.capitalize() for p in nome.split()) if nome.isupper() else nome


def _representantes(de_para: dict) -> tuple[dict, list]:
    """{nome normalizado → Representante}; cria (ativo) quem não existe no cadastro. Devolve (mapa, criados)."""
    # Nomes repetidos no cadastro: vale o ativo que tem cartão (depois o mais antigo).
    cadastro = {}
    for r in (Representante.objects.annotate(n_cartoes=Count("cartoes"))
              .order_by("-ativo", "-n_cartoes", "pk")):
        cadastro.setdefault(normalizar_texto(r.nome), r)
    criados = []

    def obter(nome_bruto):
        chave = normalizar_texto(nome_bruto)
        canonico = de_para.get(chave)
        alvo = cadastro.get(normalizar_texto(canonico)) if canonico else cadastro.get(chave)
        if alvo is None:
            alvo = Representante.objects.create(nome=canonico or _titulo(nome_bruto))
            cadastro[normalizar_texto(alvo.nome)] = alvo
            criados.append(alvo.nome)
        cadastro.setdefault(chave, alvo)
        return alvo
    return obter, criados


def importar_base(origem, nome_original: str, *, usuario="", ip=None, maquina="") -> tuple[Importacao, bool]:
    """Importa a planilha geral como nova versão da base (a anterior fica inativa). Mesmo arquivo: nada muda."""
    hash_arquivo, caminho = arquivos.guardar(origem, nome_original, subpasta="base", extensoes={".xlsx"},
                                             assinatura=ASSINATURA_XLSX, descricao="uma planilha .xlsx")
    ativa = Importacao.objects.filter(tipo=Importacao.Tipo.BASE, ativo=True, hash_sha256=hash_arquivo).first()
    if ativa:
        return ativa, False
    try:
        livro = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise BaseInvalida(f"Não foi possível abrir a planilha ({exc.__class__.__name__}).") from exc
    abas = sorted(livro.worksheets, key=lambda a: a.title != "ATUALIZADOS")
    aba, cab = None, None
    for candidata in abas:
        primeira = next(candidata.iter_rows(max_row=1, values_only=True), ())
        nomes = [_texto(c) for c in primeira]
        if all(c in nomes for c in COLUNAS_OBRIGATORIAS):
            aba, cab = candidata, {c: i for i, c in enumerate(nomes) if c}
            break
    if aba is None:
        raise BaseInvalida("Nenhuma aba tem as colunas Nome da Origem, Data, Despesa e Valor.")

    params = parametros.obter_todos()
    obter_rep, criados = _representantes(params["de_para_representantes"])
    pega = lambda linha, col: linha[cab[col]] if col in cab and cab[col] < len(linha) else None  # noqa: E731
    hoje = timezone.localdate()
    linhas, ignoradas = [], {"em branco": 0, "sem valor": 0, "sem data": 0, "data fora do período": 0}
    with transaction.atomic():
        ultima = Importacao.objects.filter(tipo=Importacao.Tipo.BASE).aggregate(v=Max("versao"))["v"] or 0
        imp = Importacao.objects.create(tipo=Importacao.Tipo.BASE, versao=ultima + 1, origem=Importacao.Origem.UPLOAD,
                                        status=Importacao.Status.PROCESSANDO, arquivo_caminho=str(caminho),
                                        arquivo_nome_original=nome_original[:255], hash_sha256=hash_arquivo,
                                        usuario=usuario, ip=ip, maquina=maquina)
        for n, linha in enumerate(aba.iter_rows(min_row=2, values_only=True), start=2):
            nome, valor, data = pega(linha, "Nome da Origem"), pega(linha, "Valor"), pega(linha, "Data")
            if not _texto(nome) and valor in (None, ""):
                ignoradas["em branco"] += 1
                continue
            try:
                valor = Decimal(str(valor)).quantize(Decimal("0.01"))
            except (InvalidOperation, ValueError):
                ignoradas["sem valor"] += 1
                continue
            if isinstance(data, dt.datetime):
                data = data.date()
            if not isinstance(data, dt.date):
                ignoradas["sem data"] += 1
                continue
            if data.year < 2000 or data > hoje:
                ignoradas["data fora do período"] += 1
                continue
            rep = obter_rep(nome) if _texto(nome) else None
            despesa = _texto(pega(linha, "Despesa"))
            linhas.append(GastoBase(
                importacao=imp, linha=n, representante=rep, nome_origem=_texto(nome)[:120], data=data,
                despesa_original=despesa[:120], categoria=categoria(despesa, params["categorias_analise"]),
                descricao=_texto(pega(linha, "Descrição")), finalidade=_texto(pega(linha, "Finalidade"))[:120],
                pagamento=_texto(pega(linha, "Pagamento"))[:60], medico=_texto(pega(linha, "Médico"))[:200],
                paciente=_texto(pega(linha, "Paciente"))[:200], cidade=_texto(pega(linha, "Cidade"))[:120],
                valor=valor))
        if not linhas:
            raise BaseInvalida("A planilha não tem nenhuma despesa com data e valor.")
        GastoBase.objects.bulk_create(linhas, batch_size=2000)
        Importacao.objects.filter(tipo=Importacao.Tipo.BASE, ativo=True).update(ativo=False)
        imp.status = Importacao.Status.OK
        imp.ativo = True
        imp.ativado_em = timezone.now()
        imp.resumo = {"linhas": len(linhas), "ignoradas": ignoradas, "representantes_criados": criados,
                      "aba": aba.title, "total": str(sum((g.valor for g in linhas), Decimal("0")))}
        imp.save()
    auditoria.registrar(None, "IMPORTACAO", "OK", importacao=imp, usuario=usuario, ip=ip,
                        detalhe={k: v for k, v in imp.resumo.items() if k != "aba"})
    log.info("base geral importada", extra={"importacao_id": imp.pk, "linhas": len(linhas)})
    return imp, True


# ---------- dados unificados ----------

def _cobertura():
    """{representante_id: [(início, fim), …]} dos períodos com planilha enviada no sistema."""
    cobertura = {}
    for imp in (Importacao.objects.filter(tipo=Importacao.Tipo.PLANILHA, ativo=True, competencia__isnull=False)
                .select_related("competencia")):
        c = imp.competencia
        cobertura.setdefault(imp.representante_id, []).append((c.periodo_ini, c.fechamento))
    return cobertura


def _coberto(intervalos, d):
    return any(ini <= d <= fim for ini, fim in intervalos)


# Cartões fora do escopo de conciliação que mesmo assim entram na análise (decisão de 26/10/2026: os do João, com
# compras que precisam ser vistas, como passagens aéreas). Final do cartão; para mudar, edite esta lista.
CARTOES_EXTRA_ANALISE = ("5507", "5504", "5506", "5505", "5508", "5509")

FONTE_FATURA = "Fatura"
FONTE_REEMBOLSO = "Reembolsado (fora da fatura)"
FONTE_NAO_ENCONTRADO = "Não encontrado na fatura"
FONTE_BASE = "Base geral"
FONTES = (FONTE_FATURA, FONTE_REEMBOLSO, FONTE_NAO_ENCONTRADO, FONTE_BASE)


def coletar() -> dict:
    """Todos os gastos, um dicionário por linha. REGRA (26/10/2026): a FATURA é a fonte da verdade.

    - Uma linha por lançamento da fatura (cartões dos representantes + CARTOES_EXTRA_ANALISE), sempre com o VALOR
      DA FATURA. O que o representante declarou (ou o financeiro escolheu) só complementa: categoria, médico,
      paciente, finalidade, evento. Valor informado e diferença ficam ao lado; "divergente" quando a diferença não
      se explica por parcelamento ou conta dividida. Sem formulário = "Sem justificativa" (entra no total).
    - Reembolso (pago pelo representante, não está na fatura): linha à parte, fora do total da fatura.
    - Formulário sem lançamento na fatura: "Não encontrado na fatura", aparece na lista mas NÃO entra no total.
    - Base geral (histórico de antes do sistema): só nos períodos sem planilha no sistema.
    `mes` é o mês da FATURA (competência), usado pelos filtros de período; `data` é a data da compra."""
    from django.db.models import Q

    from .models import Cartao, FaturaLanc, FinalidadeLancamento, RespostaPendencia, chave_lancamento, rotulo_situacao
    S, R = Resultado.Status, Resultado.Regra
    de_para = parametros.obter_todos()["categorias_analise"]
    corrigidos = dict(CorrecaoValor.objects.filter(ativo=True).values_list("chave", "valor_corrigido"))
    outras = {(c.chave, c.campo): c.novo for c in CorrecaoDespesa.objects.filter(ativo=True)}
    finalidades = {f.chave: f for f in FinalidadeLancamento.objects.filter(ativo=True).select_related("evento")}
    respostas = {r.chave: r for r in RespostaPendencia.objects.filter(ativo=True, aprovado_em__isnull=False,
                                                                       nao_reconhece=False)}
    CAT = CorrecaoDespesa.Campo.CATEGORIA

    def escolha(chave, texto_formulario=""):
        f = finalidades.get(chave)
        if f is None:
            return texto_formulario or "", ""
        return f.get_finalidade_display(), (f.evento.nome if f.evento else "")

    linhas = []

    # 1) Fatura: o valor que vale
    lancs = (FaturaLanc.objects.filter(importacao__tipo=Importacao.Tipo.FATURA, importacao__ativo=True)
             .exclude(secao=FaturaLanc.Secao.CONTA)
             .filter(Q(cartao__escopo=Cartao.Escopo.CONCILIAR_FORMULARIO, cartao__representante__isnull=False)
                     | Q(cartao__final__in=CARTOES_EXTRA_ANALISE))
             .select_related("cartao__representante", "importacao__competencia"))
    por_lanc = {}
    for r in Resultado.objects.filter(vigente=True, fatura_lanc__isnull=False).select_related("form_despesa"):
        por_lanc.setdefault(r.fatura_lanc_id, []).append(r)
    for l in lancs:
        rs = por_lanc.get(l.pk, [])
        status, regra = (rs[0].status, rs[0].regra) if rs else ("", "")
        forms = list({r.form_despesa_id: r.form_despesa for r in rs if r.form_despesa_id}.values())
        chave_l = chave_lancamento(l)
        rep = l.cartao.representante.nome if l.cartao.representante else _titulo(l.cartao.nome_fatura)
        comum = {"fonte": FONTE_FATURA, "rep": rep, "nome_origem": rep, "data": l.data,
                 "mes": l.importacao.competencia.ano_mes, "pagamento": "Cartão Crédito",
                 "situacao": rotulo_situacao(status), "conta_total": True, "valor_fatura": l.valor}
        if not forms:
            resp = respostas.get(chave_l)
            if resp is not None:
                despesa, origem = resp.categoria, "Respondido pelo link"
                desc, med, pac, cid = resp.descricao, resp.medico, resp.paciente, resp.cidade or l.cidade
            else:
                despesa = outras.get((chave_l, CAT)) or "Não informado"
                origem = "Escolha do financeiro" if (chave_l, CAT) in outras else "Sem justificativa"
                desc, med, pac, cid = l.descricao, "", "", l.cidade
            fin, evento = escolha(chave_l)
            linhas.append({**comum, "ref": f"L{l.pk}", "despesa": despesa, "categoria": categoria(despesa, de_para),
                           "descricao": desc, "finalidade": fin, "evento": evento, "medico": med, "paciente": pac,
                           "cidade": cid, "valor": l.valor, "valor_informado": None, "diferenca": None,
                           "divergente": False, "origem": origem})
            continue
        # Com formulário: conta dividida (vários formulários num lançamento) reparte pelo informado e a última
        # parte fecha no valor da fatura, para a soma nunca sair do que foi faturado.
        restante = l.valor
        for i, d in enumerate(forms):
            chave_d = chave_despesa(d)
            informado = corrigidos.get(chave_d, d.valor)
            valor = restante if i == len(forms) - 1 else (informado or Decimal("0"))
            restante -= valor
            diferenca = None if informado is None else informado - valor
            divergente = bool(diferenca) and regra not in (R.PARCELADO, R.DIVIDIDA)
            despesa = outras.get((chave_d, CAT), d.despesa)
            fin, evento = escolha(chave_l, d.finalidade)
            linhas.append({**comum, "ref": f"L{l.pk}F{d.pk}", "despesa": despesa,
                           "categoria": categoria(despesa, de_para), "descricao": d.descricao, "finalidade": fin,
                           "evento": evento, "medico": d.medico_canonico or d.medico_original, "paciente": d.paciente,
                           "cidade": d.cidade or l.cidade, "valor": valor, "valor_informado": informado,
                           "diferenca": diferenca, "divergente": divergente, "origem": "Formulário"})

    # 2) Reembolso e 3) formulário não encontrado na fatura: aparecem, mas fora do total da fatura
    soltos = (Resultado.objects.filter(vigente=True, fatura_lanc__isnull=True, form_despesa__isnull=False,
                                       status__in=[S.REEMBOLSO, S.SEM_COBRANCA, S.PENDENTE_ANALISE])
              .select_related("form_despesa__representante", "competencia"))
    vistos = set()
    for r in soltos:
        d = r.form_despesa
        if d.pk in vistos or d.duplicado:
            continue
        vistos.add(d.pk)
        chave_d = chave_despesa(d)
        informado = corrigidos.get(chave_d, d.valor)
        if informado is None:
            continue   # valor ilegível: fica na análise da conciliação até alguém corrigir
        nova_data = outras.get((chave_d, CorrecaoDespesa.Campo.DATA))
        data = dt.date.fromisoformat(nova_data) if nova_data else (d.data_efetiva or d.carimbo.date())
        despesa = outras.get((chave_d, CAT), d.despesa)
        reembolso = r.status == S.REEMBOLSO
        fin, evento = escolha(chave_d, d.finalidade)
        linhas.append({
            "fonte": FONTE_REEMBOLSO if reembolso else FONTE_NAO_ENCONTRADO, "ref": f"F{d.pk}",
            "rep": d.representante.nome, "nome_origem": d.representante.nome, "data": data,
            "mes": r.competencia.ano_mes, "despesa": despesa, "categoria": categoria(despesa, de_para),
            "descricao": d.descricao, "finalidade": fin, "evento": evento,
            "pagamento": "Reembolso" if reembolso else "Cartão Crédito",
            "medico": d.medico_canonico or d.medico_original, "paciente": d.paciente, "cidade": d.cidade,
            "valor": informado, "valor_fatura": None, "valor_informado": informado, "diferenca": None,
            "divergente": False, "situacao": rotulo_situacao(r.status), "conta_total": False, "origem": "Formulário"})

    # 4) Base geral (histórico): só onde não há planilha do sistema
    base = Importacao.objects.filter(tipo=Importacao.Tipo.BASE, ativo=True).first()
    substituidas = 0
    if base:
        cobertura = _cobertura()
        for g in GastoBase.objects.filter(importacao=base).select_related("representante").order_by("data", "linha"):
            if g.representante_id in cobertura and _coberto(cobertura[g.representante_id], g.data):
                substituidas += 1
                continue
            reembolso = _pagamento(g.pagamento) == "Reembolso"
            linhas.append({
                "fonte": FONTE_BASE, "ref": f"B{g.pk}", "rep": g.representante.nome if g.representante else g.nome_origem,
                "nome_origem": g.nome_origem, "data": g.data, "mes": f"{g.data:%Y-%m}", "despesa": g.despesa_original,
                "categoria": g.categoria, "descricao": g.descricao, "finalidade": g.finalidade, "evento": "",
                "pagamento": g.pagamento, "medico": g.medico, "paciente": g.paciente, "cidade": g.cidade,
                "valor": g.valor, "valor_fatura": None, "valor_informado": g.valor, "diferenca": None,
                "divergente": False, "situacao": "", "conta_total": not reembolso, "origem": "Base geral"})

    linhas.sort(key=lambda x: (x["mes"], x["data"], x["rep"]))
    return {"linhas": linhas, "base": base, "substituidas_pela_planilha": substituidas}


def totais(linhas) -> dict:
    """Total na fatura (o que vale) e, à parte, reembolsado e não encontrado na fatura. Sempre em Decimal."""
    zero = Decimal("0")
    return {"fatura": sum((l["valor"] for l in linhas if l["conta_total"]), zero),
            "reembolso": sum((l["valor"] for l in linhas if l["fonte"] == FONTE_REEMBOLSO
                              or (l["fonte"] == FONTE_BASE and not l["conta_total"])), zero),
            "nao_encontrado": sum((l["valor"] for l in linhas if l["fonte"] == FONTE_NAO_ENCONTRADO), zero)}


def _finalidade(texto):
    t = normalizar_texto(texto)
    if not t:
        return "Não informada"
    for chave, rotulo in (("negoci", "Negociação"), ("procediment", "Procedimento"), ("evento", "Evento")):
        if t.startswith(chave):
            return rotulo
    return "Outras"


def _pagamento(texto):
    return "Reembolso" if normalizar_texto(texto).startswith("reembolso") else "Cartão"


def dados_dashboard(coletado: dict) -> dict:
    """Formato compacto para o dashboard (sem paciente)."""
    # Fora do dashboard: formulário não encontrado na fatura (não entra em total nenhum; aparece na lista geral).
    # O valor vai em centavos (inteiro) para a tela somar sem erro de arredondamento.
    colunas = ["rep", "data", "tipo", "finalidade", "pagamento", "centavos", "cidade", "descricao", "medico",
               "tipo_original", "fonte", "mes", "evento"]
    linhas = [[l["rep"], l["data"].isoformat(), l["categoria"], _finalidade(l["finalidade"]), _pagamento(l["pagamento"]),
               int((l["valor"] * 100).to_integral_value()), _titulo(l["cidade"])[:40], l["descricao"][:40],
               _titulo(l["medico"])[:40], l["despesa"][:40], l["fonte"], l["mes"], l["evento"]]
              for l in coletado["linhas"] if l["fonte"] != FONTE_NAO_ENCONTRADO]
    base = coletado["base"]
    return {"gerado_em": timezone.localdate().isoformat(),
            "fonte": "faturas do sistema" + (f" e base geral v{base.versao}" if base else ""),
            "mes_atual": timezone.localdate().strftime("%Y-%m"),
            "colunas": colunas, "linhas": linhas,
            "excluidas": (base.resumo.get("ignoradas", {}) if base else {}),
            "substituidas": coletado["substituidas_pela_planilha"],
            "categorias": parametros.CATEGORIAS_ANALISE}


def exportar_xlsx(linhas: list) -> bytes:
    """Lista geral no layout da GERAL REPRESENTANTES, com as colunas de controle no fim."""
    livro = openpyxl.Workbook()
    aba = livro.active
    aba.title = "ATUALIZADOS"
    aba.append(COLUNAS_EXPORTACAO)
    for celula in aba[1]:
        celula.font = Font(bold=True, color="FFFFFF")
        celula.fill = PatternFill("solid", fgColor="0E5A62")
    for l in linhas:
        # valores em Decimal (o openpyxl grava o número exato, sem passar por float)
        aba.append([l["nome_origem"] or l["rep"], MESES[l["data"].month - 1], l["data"], l["despesa"], l["descricao"],
                    l["finalidade"], l["pagamento"], l["medico"], l["paciente"], l["cidade"], l["valor"],
                    l["data"].year, l["categoria"], l["fonte"], l["situacao"],
                    f"{l['mes'][5:]}/{l['mes'][:4]}", l["evento"], l["valor_informado"], l["diferenca"],
                    "sim" if l["divergente"] else "", "sim" if l["conta_total"] else "não"])
    for i, largura in enumerate([16, 11, 11, 22, 36, 16, 16, 24, 24, 18, 12, 6, 22, 18, 24, 12, 26, 14, 12, 10, 12],
                                start=1):
        aba.column_dimensions[get_column_letter(i)].width = largura
    for (celula_data,), (celula_valor,) in zip(aba.iter_rows(min_row=2, min_col=3, max_col=3),
                                                aba.iter_rows(min_row=2, min_col=11, max_col=11)):
        celula_data.number_format = "DD/MM/YYYY"
        celula_valor.number_format = '#,##0.00'
    for coluna in aba.iter_rows(min_row=2, min_col=18, max_col=19):   # valor informado e diferença
        for celula in coluna:
            celula.number_format = '#,##0.00'
    aba.freeze_panes = "A2"
    aba.auto_filter.ref = aba.dimensions
    saida = io.BytesIO()
    livro.save(saida)
    return saida.getvalue()
