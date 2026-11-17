"""Motor de conciliação: fatura × formulário, por competência, com trava de fechamento.

Ordem (cada item casa no máximo uma vez, exceto o formulário de compra parcelada, que cobre as N parcelas):
 0. fora da conciliação individual: seção CONTA → ENCARGOS_CONTA; cartões FORA_ESCOPO/FASE2_NF → FORA_ESCOPO;
    compra em moeda estrangeira → PENDENTE_ANALISE (decisão de 06/10/2026);
 1. estorno: negativo que anula um positivo do mesmo cartão e valor → ESTORNO_COMPENSADO (antes do casamento);
 2. valor exato e data a até JANELA_DIAS (exato/janela/data corrigida), da menor distância para a maior;
 3. parcelado: parcela k/N casa com formulário de valor ≈ parcela × N (tolerância por parcela), mesma data da compra;
 4. conta dividida: até N itens de um lado (mesmo dia) somando exatamente o item do outro lado;
 5. valor exato com data mais distante, desde que o formulário seja desta competência → ressalva "data distante".
Ressalvas: data corrigida, data distante, categoria incoerente com o estabelecimento, sem recibo válido.

TRAVA: a soma de valor_trava dos resultados vigentes (lado da fatura) tem de fechar o total da fatura ao centavo.
"""
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import combinations

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from . import auditoria, controle, parametros
from .models import (AjusteManual, Anexo, Cartao, Competencia, CorrecaoDespesa, CorrecaoValor, Execucao, FaturaLanc,
                     FormDespesa, Importacao, RespostaPendencia,
                     Resultado, VinculoParcela, chave_despesa, chave_lancamento)
from .parametros import normalizar_texto

log = logging.getLogger(__name__)
S = Resultado.Status
R = Resultado.Regra
ZERO = Decimal("0")


class ConciliacaoBloqueada(Exception):
    pass


@dataclass
class Linha:
    obj: FaturaLanc
    usada: bool = False

    @property
    def valor(self):
        return self.obj.valor

    @property
    def data(self):
        return self.obj.data


@dataclass
class Form:
    obj: FormDespesa
    data: date | None
    usado: bool = False
    correcao: object = None          # CorrecaoValor ativa: o valor informado errado, corrigido por uma pessoa
    data_pessoa: object = None       # CorrecaoDespesa (DATA) ativa: a data já vem corrigida em `data`
    categoria_pessoa: object = None  # CorrecaoDespesa (CATEGORIA) ativa

    @property
    def valor(self):
        return self.correcao.valor_corrigido if self.correcao is not None else self.obj.valor

    @property
    def despesa(self):
        return self.categoria_pessoa.novo if self.categoria_pessoa is not None else self.obj.despesa


@dataclass
class Saida:
    """Um resultado a gravar."""
    status: str
    linha: Linha | None = None
    form: Form | None = None
    regra: str = ""
    score: Decimal | None = None
    grupo: uuid.UUID | None = None
    valor_trava: Decimal | None = None
    motivos: list = field(default_factory=list)
    sem_comprovante: bool = False


# ---------- utilidades ----------

def _competencia_da_data(d: date | None, competencias: list) -> Competencia | None:
    """A competência cujo período contém a data: a de início mais recente que não passa da data."""
    if d is None:
        return None
    candidata = None
    for c in competencias:
        if c.periodo_ini <= d:
            candidata = c
    if candidata and d <= candidata.fechamento:
        return candidata
    return None


competencia_da_data = _competencia_da_data


def _categoria_incoerente(despesa: str, linha: FaturaLanc, palavras: dict) -> str:
    """Estabelecimento da fatura indica outra categoria. Vale a família: "Alimentação pessoal" e "Alimentação com
    médicos" batem com "Alimentação"; "Estacionamento aeroporto" com "Estacionamento"."""
    texto = normalizar_texto(linha.descricao)
    sugeridas = [cat for cat, lista in palavras.items() if any(normalizar_texto(p) in texto for p in lista)]
    informada = normalizar_texto(despesa)
    if not sugeridas or any(informada.startswith(normalizar_texto(c)) for c in sugeridas):
        return ""
    return f"lançado como {despesa or '—'}, estabelecimento parece {', '.join(sugeridas)}"


def _sem_recibo(form: FormDespesa, linha: FaturaLanc | None = None, anexos: frozenset = frozenset()) -> bool:
    """Sem link válido na planilha e sem comprovante anexado pela tela (na resposta ou no lançamento).
    A planilha geral é montada e conferida pelo financeiro: vale como comprovada (decisão de 22/10/2026)."""
    if form.link_comprovante.strip().lower().startswith("http"):
        return False
    if (form.importacao.resumo or {}).get("planilha_geral"):
        return False
    if chave_despesa(form) in anexos:
        return False
    return not (linha is not None and chave_lancamento(linha) in anexos)


def _score(dias: int) -> Decimal:
    return max(Decimal("100") - Decimal(10) * dias, Decimal("10"))


# ---------- casamento de um representante ----------

CASADO = (S.CONCILIADO, S.CONCILIADO_RESSALVA)


def _conciliar_representante(linhas, forms, competencia, competencias, params, consumidos, anexos=frozenset(),
                             casados_fora=None):
    """casados_fora: {form_despesa_id: "Ailos 2026-10"} formulários já casados com a fatura de outra competência."""
    casados_fora = casados_fora or {}
    saidas = []
    janela = params["janela_dias"]
    tolerancia = Decimal(params["tolerancia_parcela_reais"])
    max_itens = params["max_itens_conta_dividida"]
    palavras = params["palavras_categoria"]
    ciclos = params["ciclos_pendente_analise"]

    # Moeda estrangeira: análise (sem regra 5 por decisão de 06/10/2026)
    for l in linhas:
        if l.obj.moeda != "BRL":
            l.usada = True
            saidas.append(Saida(S.PENDENTE_ANALISE, linha=l, valor_trava=l.valor,
                                motivos=["compra em moeda estrangeira"]))

    # 1. Estorno antes do casamento
    negativos = [l for l in linhas if not l.usada and l.valor < 0]
    for n in negativos:
        pares = [p for p in linhas if not p.usada and p.valor == -n.valor and p.obj.cartao_id == n.obj.cartao_id]
        if not pares:
            continue
        p = min(pares, key=lambda p: (abs((p.data - n.data).days), p.obj.ordem))
        grupo = uuid.uuid4()
        n.usada = p.usada = True
        for l in (p, n):
            saidas.append(Saida(S.ESTORNO_COMPENSADO, linha=l, regra=R.ESTORNO, grupo=grupo, valor_trava=l.valor,
                                score=Decimal("100"), motivos=["compra e estorno na mesma fatura"]))

    # Formulários: o que não entra no casamento já recebe o status
    elegiveis = []
    for f in forms:
        d = f.obj
        pertence = _competencia_da_data(f.data or d.carimbo.date(), competencias)
        desta = pertence is not None and pertence.pk == competencia.pk
        motivo_fora = None
        if normalizar_texto(d.pagamento).startswith("reembolso"):
            motivo_fora = (S.REEMBOLSO, "pago pelo representante (reembolso)")
        elif d.duplicado:
            motivo_fora = (S.DUPLICADO_FORM, "mesma data, valor e despesa de outra resposta")
        elif d.pagamento_outro_cartao:
            motivo_fora = (S.FORA_ESCOPO, f"pagamento informado: {d.pagamento}")
        elif d.moeda_estrangeira and f.correcao is None:
            motivo_fora = (S.PENDENTE_ANALISE, f"valor em moeda estrangeira: {d.valor_original}")
        elif d.valor_invalido and f.correcao is None:
            motivo_fora = (S.PENDENTE_ANALISE, f"valor ilegível: {d.valor_original or 'vazio'}")
        elif f.data is None:
            motivo_fora = (S.PENDENTE_ANALISE, "data divergente do carimbo, sem correção possível")
        if motivo_fora:
            if desta:
                saidas.append(Saida(motivo_fora[0], form=f, motivos=[motivo_fora[1]]))
            continue
        if d.pk in consumidos:
            continue
        elegiveis.append(f)

    def casar(l, f, regra, dias, motivos=None):
        l.usada = True
        f.usado = True
        motivos = list(motivos or [])
        if f.obj.data_suspeita and f.data_pessoa is None:
            motivos.append(f"data corrigida de {f.obj.data_informada:%d/%m/%Y} para {f.data:%d/%m/%Y}"
                           if f.obj.data_informada else "data corrigida")
            regra = R.DATA_CORRIGIDA if regra in (R.EXATO, R.JANELA) else regra
        if dias > janela:
            motivos.append(f"data a {dias} dias da fatura")
        incoerente = _categoria_incoerente(f.despesa, l.obj, palavras)
        if incoerente:
            motivos.append(incoerente)
        sem = _sem_recibo(f.obj, l.obj, anexos)
        if sem:
            motivos.append("sem recibo válido")
        status = S.CONCILIADO_RESSALVA if motivos else S.CONCILIADO
        # correções feitas pelo financeiro não viram ressalva por si sós; ficam registradas no motivo
        if f.correcao is not None:
            c = f.correcao
            motivos.append(f"valor declarado corrigido de {c.valor_original_texto or c.valor_original or 'vazio'} "
                           f"para {c.valor_corrigido} por {c.usuario or '—'}: {c.motivo}")
        for c in (f.data_pessoa, f.categoria_pessoa):
            if c is not None:
                motivos.append(f"{c.get_campo_display().lower()} do formulário corrigida de {c.original or 'vazio'} "
                               f"para {c.novo} por {c.usuario or '—'}" + (f": {c.motivo}" if c.motivo else ""))
        saidas.append(Saida(status, linha=l, form=f, regra=regra, score=_score(dias), valor_trava=l.valor,
                            motivos=motivos, sem_comprovante=sem))

    abertas = lambda: [l for l in linhas if not l.usada and l.valor > 0]  # noqa: E731
    livres = lambda: [f for f in elegiveis if not f.usado]  # noqa: E731

    # 2. Valor exato, data na janela (menor distância primeiro)
    pares = [(abs((l.data - f.data).days), l.obj.ordem, f.obj.carimbo, l, f)
             for l in abertas() for f in livres() if l.valor == f.valor and abs((l.data - f.data).days) <= janela]
    for dias, _, _, l, f in sorted(pares, key=lambda p: p[:3]):
        if not l.usada and not f.usado:
            casar(l, f, R.EXATO if dias == 0 else R.JANELA, dias)

    # 3. Parcelado: k/N casa com o total da compra (mesma data da compra, que a fatura repete em cada parcela)
    pares = []
    for l in abertas():
        n = l.obj.parcela_total
        if not n or n < 2:
            continue
        for f in livres():
            dias = abs((l.data - f.data).days)
            if dias <= janela and abs(f.valor - l.valor * n) <= tolerancia * n:
                pares.append((dias, l.obj.ordem, f.obj.carimbo, l, f))
    vinculos = []
    for dias, _, _, l, f in sorted(pares, key=lambda p: p[:3]):
        if not l.usada and not f.usado:
            casar(l, f, R.PARCELADO, dias)
            if l.obj.parcela_atual and l.obj.parcela_atual >= 2:
                vinculos.append((l, f))

    # 4. Conta dividida: até max_itens formulários do mesmo dia = 1 lançamento, e o inverso
    for l in abertas():
        mesmos = [f for f in livres() if abs((l.data - f.data).days) <= janela]
        por_dia = {}
        for f in mesmos:
            por_dia.setdefault(f.data, []).append(f)
        achado = None
        for dia in sorted(por_dia, key=lambda d: abs((l.data - d).days)):
            candidatos = sorted(por_dia[dia], key=lambda f: f.obj.carimbo)
            for k in range(2, max_itens + 1):
                achado = next((c for c in combinations(candidatos, k) if sum(f.valor for f in c) == l.valor), None)
                if achado:
                    break
            if achado:
                break
        if achado:
            grupo = uuid.uuid4()
            l.usada = True
            for i, f in enumerate(achado):
                f.usado = True
                motivos = [f"conta dividida em {len(achado)} formulários"]
                sem = _sem_recibo(f.obj, l.obj, anexos)
                if sem:
                    motivos.append("sem recibo válido")
                saidas.append(Saida(S.CONCILIADO_RESSALVA, linha=l, form=f, regra=R.DIVIDIDA, grupo=grupo,
                                    score=Decimal("80"), valor_trava=l.valor if i == 0 else None, motivos=motivos,
                                    sem_comprovante=sem))
    for f in livres():
        mesmos = [l for l in abertas() if abs((l.data - f.data).days) <= janela]
        por_dia = {}
        for l in mesmos:
            por_dia.setdefault((l.obj.cartao_id, l.data), []).append(l)
        achado = None
        for chave in sorted(por_dia, key=lambda c: abs((c[1] - f.data).days)):
            candidatos = sorted(por_dia[chave], key=lambda l: l.obj.ordem)
            for k in range(2, max_itens + 1):
                achado = next((c for c in combinations(candidatos, k) if sum(l.valor for l in c) == f.valor), None)
                if achado:
                    break
            if achado:
                break
        if achado:
            grupo = uuid.uuid4()
            f.usado = True
            for l in achado:
                l.usada = True
                motivos = [f"formulário somando {len(achado)} lançamentos"]
                sem = _sem_recibo(f.obj, l.obj, anexos)
                if sem:
                    motivos.append("sem recibo válido")
                saidas.append(Saida(S.CONCILIADO_RESSALVA, linha=l, form=f, regra=R.DIVIDIDA, grupo=grupo,
                                    score=Decimal("80"), valor_trava=l.valor, motivos=motivos,
                                    sem_comprovante=sem))

    # 5. Valor exato, data distante: só com formulário desta competência
    desta = [f for f in livres() if (c := _competencia_da_data(f.data, competencias)) and c.pk == competencia.pk]
    pares = [(abs((l.data - f.data).days), l.obj.ordem, f.obj.carimbo, l, f)
             for l in abertas() for f in desta if l.valor == f.valor]
    for dias, _, _, l, f in sorted(pares, key=lambda p: p[:3]):
        if not l.usada and not f.usado:
            casar(l, f, R.JANELA, dias)

    # Sobras
    for l in linhas:
        if l.usada:
            continue
        if l.valor < 0:
            saidas.append(Saida(S.PENDENTE_ANALISE, linha=l, valor_trava=l.valor,
                                motivos=["crédito sem compra correspondente nesta fatura"]))
        else:
            saidas.append(Saida(S.SEM_FORMULARIO, linha=l, valor_trava=l.valor))
    for f in elegiveis:
        if f.usado:
            continue
        pertence = _competencia_da_data(f.data, competencias)
        if pertence is None or pertence.pk != competencia.pk:
            continue
        if f.obj.pk in casados_fora:
            # a cobrança caiu na fatura de outro mês e já casou lá: aqui não é pendência
            saidas.append(Saida(S.CONCILIADO, form=f, motivos=[f"conciliado na fatura {casados_fora[f.obj.pk]}"]))
            continue
        seguintes = sum(1 for c in competencias if c.periodo_ini > competencia.fechamento)
        if seguintes >= ciclos:
            saidas.append(Saida(S.PENDENTE_ANALISE, form=f,
                                motivos=[f"no formulário como cartão e sem cobrança em {seguintes} faturas seguintes"]))
        else:
            saidas.append(Saida(S.SEM_COBRANCA, form=f, motivos=["no formulário como cartão; aguardando a próxima fatura"]))
    return saidas, vinculos


# ---------- execução ----------

def _consumidos(competencia, competencias_antes):
    """Formulários já casados (fora de parcelamento) em resultados vigentes de outras competências."""
    filtro = Q(competencia__status=Competencia.Status.FECHADA) | Q(competencia__in=competencias_antes)
    return set(Resultado.objects.filter(filtro, vigente=True, form_despesa__isnull=False)
               .exclude(competencia=competencia)
               .filter(status__in=[S.CONCILIADO, S.CONCILIADO_RESSALVA])
               .exclude(regra=R.PARCELADO)
               .values_list("form_despesa_id", flat=True))


def _casados_em_outras(competencia) -> dict:
    """{form_despesa_id: "Ailos 2026-10"} formulários casados com lançamento da fatura de OUTRA competência."""
    rotulo = {c.pk: str(c) for c in Competencia.objects.select_related("conta")}
    return {fid: rotulo.get(cid, "") for fid, cid in
            Resultado.objects.filter(vigente=True, form_despesa__isnull=False, fatura_lanc__isnull=False,
                                     status__in=CASADO).exclude(competencia=competencia)
            .values_list("form_despesa_id", "competencia_id")}


def _parcelas_de_compra_conciliada(saidas, fatura) -> None:
    """Regra do financeiro (26/10/2026): compra parcelada conciliada num mês vale para as parcelas seguintes.
    Parcela k/N sem formulário (ou aguardando planilha) vira CONCILIADO se uma parcela anterior da mesma compra
    (mesmo cartão, data da compra, valor e N) está conciliada na fatura de outro mês."""
    alvos = [s for s in saidas if s.linha is not None and s.status in (S.SEM_FORMULARIO, S.AGUARDANDO_PLANILHA)
             and (s.linha.obj.parcela_total or 0) >= 2 and (s.linha.obj.parcela_atual or 0) >= 2]
    if not alvos:
        return
    chave = lambda l: (l.cartao_id, l.data, l.valor, l.parcela_total)  # noqa: E731
    outras = (FaturaLanc.objects.filter(importacao__tipo=Importacao.Tipo.FATURA, importacao__ativo=True,
                                        cartao_id__in={s.linha.obj.cartao_id for s in alvos}, parcela_total__gte=2)
              .exclude(importacao=fatura))
    por_compra = {}
    for l in outras:
        por_compra.setdefault(chave(l), []).append(l)
    ids = [l.pk for lista in por_compra.values() for l in lista]
    conciliadas = dict(Resultado.objects.filter(vigente=True, fatura_lanc_id__in=ids, status__in=CASADO)
                       .values_list("fatura_lanc_id", "competencia_id"))
    rotulo = {c.pk: str(c) for c in Competencia.objects.filter(pk__in=set(conciliadas.values())).select_related("conta")}
    for s in alvos:
        atual = s.linha.obj
        anteriores = sorted((l for l in por_compra.get(chave(atual), [])
                             if l.parcela_atual and l.parcela_atual < atual.parcela_atual and l.pk in conciliadas),
                            key=lambda l: l.parcela_atual)
        if anteriores:
            base = anteriores[0]
            s.status, s.regra = S.CONCILIADO, R.PARCELADO
            s.motivos = [f"parcela {atual.parcela_atual}/{atual.parcela_total} de compra já conciliada na fatura "
                         f"{rotulo.get(conciliadas[base.pk], '')} (parcela {base.parcela_atual}/{base.parcela_total})"]


def executar(competencia: Competencia, cartoes=None, *, motivo="", usuario="", competencias_antes=None,
             cascata=True) -> Execucao:
    """Concilia uma competência (todos os cartões, ou só os informados) e aplica a trava.

    competencias_antes: competências abertas com prioridade sobre os formulários. Sem informar, valem todas as
    abertas mais antigas (a mesma ordem de executar_abertas), para um formulário nunca casar em dois meses.
    cascata: depois de gravar, recalcula as outras competências abertas que mudam por causa desta (formulário
    usado aqui e lá, rótulo "conciliado na fatura X", parcelas seguintes de compra conciliada)."""
    if competencia.fechada:
        raise ConciliacaoBloqueada(f"A competência {competencia} está FECHADA.")
    if competencias_antes is None:
        competencias_antes = list(Competencia.objects.filter(status=Competencia.Status.ABERTA,
                                                             periodo_ini__lt=competencia.periodo_ini))
    casados_antes = set(Resultado.objects.filter(competencia=competencia, vigente=True, form_despesa__isnull=False,
                                                 fatura_lanc__isnull=False, status__in=CASADO)
                        .values_list("form_despesa_id", flat=True))
    params = parametros.obter_todos()
    finais = sorted(c.final for c in cartoes) if cartoes is not None else []
    execucao = Execucao.objects.create(competencia=competencia, escopo=finais, motivo=motivo[:40], usuario=usuario,
                                       parametros=params)
    fatura = Importacao.objects.filter(tipo=Importacao.Tipo.FATURA, competencia=competencia, ativo=True).first()
    if fatura is None:
        execucao.status = Execucao.Status.ERRO
        execucao.fim = timezone.now()
        execucao.resumo = {"erro": "Competência sem fatura em uso."}
        execucao.save()
        return execucao

    competencias = list(Competencia.objects.filter(conta_id=competencia.conta_id).order_by("periodo_ini"))  # períodos do mesmo banco
    consumidos = _consumidos(competencia, competencias_antes)
    casados_fora = _casados_em_outras(competencia)
    anexos =frozenset(Anexo.objects.filter(ativo=True).values_list("chave", flat=True))
    ajustes = {a.chave: a for a in AjusteManual.objects.filter(competencia=competencia, ativo=True)}
    lancs = FaturaLanc.objects.filter(importacao=fatura).select_related("cartao__representante").order_by("ordem")
    if cartoes is not None:
        lancs = lancs.filter(cartao__in=cartoes)

    saidas_por_rep = []   # (representante, cartao_unico, saidas)
    saidas_soltas = []    # conta, fora do escopo, aguardando planilha
    vinculos = []
    por_rep = {}
    linhas_controle, usadas_controle = None, set()
    for lanc in lancs:
        c = lanc.cartao
        if lanc.secao == FaturaLanc.Secao.CONTA:
            motivo_conta = {"SALDO_ANTERIOR": "saldo anterior", "PAGAMENTO": "pagamento da fatura anterior"}
            saidas_soltas.append((None, None, Saida(S.ENCARGOS_CONTA, linha=Linha(lanc), valor_trava=lanc.valor,
                                                    motivos=[motivo_conta.get(lanc.natureza, "encargo da conta")])))
        elif c.escopo == Cartao.Escopo.FASE2_NF:
            # Compras da empresa: confere com a nota fiscal anexada ao lançamento (não há formulário). A planilha de
            # controle de compras diz o produto e o número da NF; compra registrada sem NF = conciliado com ressalva.
            if linhas_controle is None:
                linhas_controle = controle.linhas_ativas()
                notas_confiaveis = set(Anexo.objects.filter(ativo=True, ressalva="").values_list("chave", flat=True))
                notas_com_ressalva = dict(Anexo.objects.filter(ativo=True).exclude(ressalva="")
                                          .values_list("chave", "ressalva"))
            # O valor manda: nota ou linha do controle com o mesmo valor concilia; se o casamento for pouco
            # confiável (só pelo valor), fica conciliado com ressalva.
            reg, aviso = controle.encontrar(lanc, linhas_controle, usadas_controle)
            if reg is not None:
                usadas_controle.add(reg.pk)
            info = [f"controle: {controle.descricao(reg)}"] if reg is not None else []
            chave = chave_lancamento(lanc)
            if chave in notas_confiaveis:
                saida = Saida(S.CONCILIADO, linha=Linha(lanc), valor_trava=lanc.valor, regra="",
                              motivos=["nota fiscal anexada"] + info)
            elif chave in notas_com_ressalva:
                saida = Saida(S.CONCILIADO_RESSALVA, linha=Linha(lanc), valor_trava=lanc.valor,
                              motivos=[notas_com_ressalva[chave]] + info)
            elif reg is not None and reg.cancelada:
                saida = Saida(S.CONCILIADO_RESSALVA, linha=Linha(lanc), valor_trava=lanc.valor,
                              motivos=["crédito de compra cancelada (controle de compras)"] + info)
            elif lanc.valor < 0:
                saida = Saida(S.PENDENTE_ANALISE, linha=Linha(lanc),
                              valor_trava=lanc.valor, motivos=["crédito no cartão: conferir a nota de devolução"])
            elif reg is not None:
                # Valor bate com o controle, mas a nota não está no sistema: conciliado sem nota (lista própria).
                sem_nota = (f"NF nº {reg.nf_numero} informada no controle, nota não enviada" if reg.nf_numero
                            else "registrado no controle de compras, sem nota fiscal")
                saida = Saida(S.CONCILIADO_RESSALVA, linha=Linha(lanc), valor_trava=lanc.valor,
                              motivos=[m for m in (aviso, sem_nota) if m] + info, sem_comprovante=True)
            else:
                saida = Saida(S.SEM_NOTA, linha=Linha(lanc), valor_trava=lanc.valor, motivos=["anexe a nota fiscal"])
            saidas_soltas.append((c.representante, c, saida))
        elif c.escopo != Cartao.Escopo.CONCILIAR_FORMULARIO:
            saidas_soltas.append((c.representante, c, Saida(S.FORA_ESCOPO, linha=Linha(lanc), valor_trava=lanc.valor,
                                                            motivos=[c.get_escopo_display()])))
        elif c.representante is None:
            saidas_soltas.append((None, c, Saida(S.AGUARDANDO_PLANILHA, linha=Linha(lanc), valor_trava=lanc.valor,
                                                 motivos=["cartão sem representante no cadastro"])))
        else:
            por_rep.setdefault(c.representante, []).append(Linha(lanc))

    reps_escopo = set(por_rep)
    if cartoes is not None:
        reps_escopo |= {c.representante for c in cartoes if c.representante and
                        c.escopo == Cartao.Escopo.CONCILIAR_FORMULARIO}
    for rep in sorted(reps_escopo, key=lambda r: r.nome):
        linhas = por_rep.get(rep, [])
        cartoes_rep = list(rep.cartoes.filter(escopo=Cartao.Escopo.CONCILIAR_FORMULARIO))
        unico = cartoes_rep[0] if len(cartoes_rep) == 1 else None
        # Uma planilha por período; a conciliação olha as respostas de todos os períodos (compra de um mês pode
        # cair na fatura do mês seguinte). Sem a planilha DESTE período, o representante aguarda planilha.
        planilhas = Importacao.objects.filter(tipo=Importacao.Tipo.PLANILHA, representante=rep, ativo=True)
        if not planilhas.filter(Q(competencia=competencia) | Q(competencia__isnull=True)).exists():
            saidas_por_rep.append((rep, unico, [Saida(S.AGUARDANDO_PLANILHA, linha=l, valor_trava=l.valor,
                                                      motivos=["planilha do período não enviada"])
                                                for l in linhas]))
            continue
        correcoes = {c.chave: c for c in CorrecaoValor.objects.filter(representante=rep, ativo=True)}
        outras = {(c.chave, c.campo): c for c in CorrecaoDespesa.objects.filter(representante=rep, ativo=True)}
        forms = []
        for d in (FormDespesa.objects.filter(importacao__in=planilhas)
                  .select_related("importacao").order_by("carimbo")):
            chave = chave_despesa(d)
            nova_data = outras.get((chave, CorrecaoDespesa.Campo.DATA))
            forms.append(Form(d, date.fromisoformat(nova_data.novo) if nova_data else d.data_corrigida,
                              correcao=correcoes.get(chave), data_pessoa=nova_data,
                              categoria_pessoa=outras.get((chave, CorrecaoDespesa.Campo.CATEGORIA))))
        saidas, vinc = _conciliar_representante(linhas, forms, competencia, competencias, params, consumidos,
                                                anexos, casados_fora)
        saidas_por_rep.append((rep, unico, saidas))
        vinculos += vinc
    _parcelas_de_compra_conciliada([s for _, _, ss in saidas_por_rep for s in ss], fatura)

    with transaction.atomic():
        Competencia.objects.select_for_update().get(pk=competencia.pk)
        antigos = Resultado.objects.filter(competencia=competencia, vigente=True)
        if cartoes is not None:
            antigos = antigos.filter(Q(cartao__in=cartoes) |
                                     Q(cartao__isnull=True, representante__in=reps_escopo, fatura_lanc__isnull=True))
        antigos.update(vigente=False)
        novos = []
        for rep, unico, saidas in saidas_por_rep:
            for s in saidas:
                novos.append(_resultado(execucao, competencia, rep, unico, s))
        for rep, cartao, s in saidas_soltas:
            novos.append(_resultado(execucao, competencia, rep, cartao, s))
        _aplicar_respostas(novos, {r.chave: r for r in RespostaPendencia.objects.filter(
            ativo=True, link__competencia=competencia)})
        _aplicar_ajustes(novos, ajustes)
        Resultado.objects.bulk_create(novos, batch_size=1000)
        VinculoParcela.objects.bulk_create([VinculoParcela(execucao=execucao, fatura_lanc=l.obj, form_despesa=f.obj)
                                            for l, f in vinculos])
        execucao.resumo = _resumo(competencia, fatura)
        execucao.status = Execucao.Status.OK if execucao.resumo["trava_ok"] else Execucao.Status.ERRO
        execucao.fim = timezone.now()
        execucao.save()

    auditoria.registrar(None, "REPROCESSAMENTO", "OK" if execucao.status == Execucao.Status.OK else "ERRO",
                        competencia=competencia, usuario=usuario or "sistema",
                        detalhe={"execucao": execucao.pk, "motivo": motivo, "escopo": finais,
                                 "diferenca_trava": execucao.resumo["diferenca"]})
    log.info("conciliação executada", extra={"execucao_id": execucao.pk, "competencia": str(competencia),
                                             "escopo": finais, "status": execucao.status})
    if cascata:
        for outra in _afetadas(competencia, fatura, casados_antes):
            executar(outra, motivo="atualização cruzada", usuario=usuario, cascata=False)
        rotular_conciliados_em_outra_fatura()
    return execucao


def rotular_conciliados_em_outra_fatura() -> int:
    """Etapa final, sobre o que já está gravado em todas as competências abertas (não depende da ordem em que os
    meses rodaram): resposta do formulário que ficou "Sem cobrança"/"Pendente de análise" no mês da planilha, mas
    está casada com a fatura de outro mês, vira CONCILIADO com o motivo "conciliado na fatura X". Linha com ajuste
    manual não é tocada. Devolve quantas linhas mudaram."""
    casados = _casados_em_outras(None)
    soltos = (Resultado.objects.filter(vigente=True, fatura_lanc__isnull=True, form_despesa_id__in=list(casados),
                                       status__in=[S.SEM_COBRANCA, S.PENDENTE_ANALISE], ajuste__isnull=True,
                                       competencia__status=Competencia.Status.ABERTA))
    mudou = 0
    for r in soltos:
        Resultado.objects.filter(pk=r.pk).update(status=S.CONCILIADO, status_automatico=S.CONCILIADO,
                                                 motivo=f"conciliado na fatura {casados[r.form_despesa_id]}")
        mudou += 1
    return mudou


def _afetadas(competencia, fatura, casados_antes) -> list:
    """Outras competências abertas que mudam por causa desta execução: as que têm resultado com um formulário
    casado aqui agora ou antes (uso em dobro, rótulo "conciliado na fatura X") e as seguintes que têm parcela de
    uma compra parcelada desta fatura. Em ordem cronológica."""
    casados = set(Resultado.objects.filter(competencia=competencia, vigente=True, form_despesa__isnull=False,
                                           fatura_lanc__isnull=False, status__in=CASADO)
                  .values_list("form_despesa_id", flat=True))
    formularios = casados | casados_antes
    ids = set(Resultado.objects.filter(vigente=True, form_despesa_id__in=formularios)
              .exclude(competencia=competencia).values_list("competencia_id", flat=True)) if formularios else set()
    compras = {(l.cartao_id, l.data, l.valor, l.parcela_total) for l in
               FaturaLanc.objects.filter(importacao=fatura, parcela_total__gte=2)}
    if compras:
        for l in (FaturaLanc.objects.filter(importacao__tipo=Importacao.Tipo.FATURA, importacao__ativo=True,
                                            parcela_total__gte=2, cartao_id__in={c[0] for c in compras},
                                            importacao__competencia__periodo_ini__gt=competencia.periodo_ini)
                  .select_related("importacao")):
            if (l.cartao_id, l.data, l.valor, l.parcela_total) in compras:
                ids.add(l.importacao.competencia_id)
    return list(Competencia.objects.filter(pk__in=ids, status=Competencia.Status.ABERTA,
                                           importacoes__tipo=Importacao.Tipo.FATURA, importacoes__ativo=True)
                .distinct().order_by("periodo_ini"))


def _resultado(execucao, competencia, rep, cartao, s: Saida) -> Resultado:
    lanc = s.linha.obj if s.linha else None
    form = s.form.obj if s.form else None
    if lanc is not None and lanc.cartao_id:
        cartao = lanc.cartao
        rep = rep or lanc.cartao.representante
    return Resultado(execucao=execucao, competencia=competencia, representante=rep or (form.representante if form else None),
                     cartao=cartao, fatura_lanc=lanc, form_despesa=form, status=s.status, regra=s.regra,
                     score=s.score, grupo=s.grupo, valor_trava=s.valor_trava, sem_comprovante=s.sem_comprovante,
                     motivo="; ".join(s.motivos), vigente=True)


def _aplicar_respostas(novos, respostas: dict):
    """Resposta do representante pelo link de pendências, para lançamento ainda sem formulário (ou aguardando
    planilha): laranja até o financeiro aprovar; aprovada, verde; "não reconheço", análise; recusada, continua
    pendente com o motivo. Se o formulário chegar depois e casar, o casamento vale e a resposta fica só de registro."""
    if not respostas:
        return
    for r in novos:
        if r.fatura_lanc is None or r.status not in (S.SEM_FORMULARIO, S.AGUARDANDO_PLANILHA):
            continue
        resp = respostas.get(chave_lancamento(r.fatura_lanc))
        if resp is None:
            continue
        quando = timezone.localtime(resp.respondido_em).strftime("%d/%m/%Y") if resp.respondido_em else ""
        resumo = "; ".join(p for p in (resp.categoria, resp.descricao[:80]) if p)
        if resp.recusado_em:
            r.motivo = f"resposta do representante recusada por {resp.recusado_por or '—'}: {resp.motivo_recusa}"
        elif resp.nao_reconhece:
            r.status = S.PENDENTE_ANALISE
            r.motivo = f"representante não reconhece este gasto ({quando})" + (f": {resp.observacao}" if resp.observacao else "")
        elif resp.aprovado_em:
            r.status = S.CONCILIADO
            r.motivo = f"respondido pelo representante ({resumo}) e aprovado por {resp.aprovado_por or '—'}"
        else:
            r.status = S.AGUARDANDO_APROVACAO
            r.motivo = f"respondido pelo representante em {quando} ({resumo}); aguardando aprovação do financeiro"


def _aplicar_ajustes(novos, ajustes: dict):
    """Situação definida manualmente prevalece sobre a automática. O valor da trava não muda: a soma continua
    fechando o total da fatura; muda só em qual grupo o valor aparece."""
    for r in novos:
        r.status_automatico = r.status
        if not ajustes:
            continue
        ajuste = ajustes.get(chave_lancamento(r.fatura_lanc) if r.fatura_lanc else chave_despesa(r.form_despesa))
        if ajuste is None:
            continue
        automatico = Resultado.Status(r.status).label
        r.ajuste = ajuste
        r.status = ajuste.status
        r.motivo = "; ".join(p for p in (f"ajuste manual de {ajuste.usuario or '—'}"
                                         f"{': ' + ajuste.observacao if ajuste.observacao else ''}",
                                         f"automático: {automatico}" + (f" ({r.motivo})" if r.motivo else "")) if p)


def _resumo(competencia, fatura) -> dict:
    """Totais por status (vigentes) e a trava de fechamento."""
    vigentes = Resultado.objects.filter(competencia=competencia, vigente=True)
    por_status = {}
    for linha in vigentes.values("status").annotate(valor=Sum("valor_trava")):
        por_status[linha["status"]] = {"valor": str(linha["valor"] or ZERO)}
    for status in por_status:
        por_status[status]["qtd"] = vigentes.filter(status=status).count()
    soma = vigentes.aggregate(t=Sum("valor_trava"))["t"] or ZERO
    lancs = FaturaLanc.objects.filter(importacao=fatura).count()
    cobertos = vigentes.filter(fatura_lanc__importacao=fatura).values("fatura_lanc").distinct().count()
    diferenca = soma - competencia.total_fatura
    return {"por_status": por_status, "soma_trava": str(soma), "total_fatura": str(competencia.total_fatura),
            "diferenca": str(diferenca), "lancamentos": lancs, "lancamentos_com_resultado": cobertos,
            "trava_ok": diferenca == 0 and cobertos == lancs}


def executar_abertas(cartoes=None, *, motivo="", usuario="") -> list:
    """Reconcilia todas as competências ABERTAS com fatura em uso, em ordem cronológica
    (as anteriores têm prioridade sobre os formulários)."""
    execucoes = []
    anteriores = []
    abertas = list(Competencia.objects.filter(status=Competencia.Status.ABERTA,
                                          importacoes__tipo=Importacao.Tipo.FATURA, importacoes__ativo=True)
               .distinct().order_by("periodo_ini"))
    for comp in abertas:
        execucoes.append(executar(comp, cartoes, motivo=motivo, usuario=usuario, competencias_antes=list(anteriores),
                                  cascata=False))
        anteriores.append(comp)
    # 2ª volta: o rótulo "conciliado na fatura X" e as parcelas dependem de meses que rodaram depois
    for comp in abertas:
        executar(comp, cartoes, motivo=motivo, usuario=usuario, competencias_antes=list(anteriores[:anteriores.index(comp)]),
                 cascata=False)
    rotular_conciliados_em_outra_fatura()
    return execucoes
