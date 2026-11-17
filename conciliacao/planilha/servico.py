"""Importação da planilha do formulário de um representante (upload .xlsx).

1. criar_importacao(): valida e guarda o arquivo; mesmo arquivo da versão ativa não cria nada.
2. processar() (segundo plano): normaliza, grava as respostas, ativa a nova versão (a anterior fica inativa,
   nada é apagado) e reconcilia só os cartões daquele representante em todas as competências abertas.
   A prévia "em uso × novo" antes de substituir entra na etapa 8.
"""
import logging

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .. import arquivos, auditoria, motor, parametros
from ..models import Cartao, Competencia, FormDespesa, Importacao, Representante
from .normalizacao import PlanilhaInvalida, Resultado, normalizar, normalizar_geral

log = logging.getLogger(__name__)

ASSINATURA_XLSX = b"PK\x03\x04"


def criar_importacao(origem, nome_original: str, representante: Representante, competencia: Competencia, *,
                     usuario="", ip=None, maquina="") -> tuple[Importacao, bool]:
    """Planilha de um representante PARA UM PERÍODO. Pode ser a planilha completa do Forms: no processamento
    só ficam as respostas cuja data cai no período da competência."""
    if competencia.fechada:
        raise arquivos.ArquivoRecusado(f"A competência {competencia} está FECHADA e não aceita planilha.")
    hash_arquivo, caminho = arquivos.guardar(origem, nome_original, subpasta="planilhas", extensoes={".xlsx"},
                                             assinatura=ASSINATURA_XLSX, descricao="uma planilha .xlsx")
    ativa = Importacao.objects.filter(tipo=Importacao.Tipo.PLANILHA, representante=representante,
                                      competencia=competencia, ativo=True, hash_sha256=hash_arquivo).first()
    if ativa:
        auditoria.registrar(None, "IMPORTACAO", "RECUSADO", representante=representante, competencia=competencia,
                            importacao=ativa, usuario=usuario, ip=ip,
                            detalhe={"motivo": "mesmo arquivo da versão ativa"})
        return ativa, False
    with transaction.atomic():
        Representante.objects.select_for_update().get(pk=representante.pk)
        ultima = Importacao.objects.filter(tipo=Importacao.Tipo.PLANILHA, representante=representante,
                                           competencia=competencia).aggregate(v=Max("versao"))["v"] or 0
        imp = Importacao.objects.create(
            tipo=Importacao.Tipo.PLANILHA, representante=representante, competencia=competencia, versao=ultima + 1,
            origem=Importacao.Origem.UPLOAD, status=Importacao.Status.PROCESSANDO, arquivo_caminho=str(caminho),
            arquivo_nome_original=nome_original[:255], hash_sha256=hash_arquivo, usuario=usuario, ip=ip,
            maquina=maquina, resumo={"progresso": {"feito": 0, "total": 2, "etapa": "na fila"}})
    return imp, True


def processar(importacao_id: int) -> Importacao:
    imp = Importacao.objects.select_related("representante").get(pk=importacao_id)
    if imp.status != Importacao.Status.PROCESSANDO:
        return imp
    rep = imp.representante
    try:
        resultado = normalizar(imp.arquivo_caminho, parametros.obter_todos())
    except (PlanilhaInvalida, parametros.ParametroInvalido) as exc:
        return _falhar(imp, str(exc))
    except Exception:  # noqa: BLE001
        log.exception("falha inesperada na leitura da planilha", extra={"importacao_id": imp.pk})
        return _falhar(imp, "Falha inesperada na leitura da planilha. Detalhes no log da aplicação.")
    if not resultado.despesas:
        return _falhar(imp, "A planilha não tem nenhuma resposta preenchida.")
    # Só as respostas do período (pela data da despesa; sem data utilizável, pelo carimbo).
    competencias = list(Competencia.objects.filter(conta_id=imp.competencia.conta_id).order_by("periodo_ini"))
    total_planilha = len(resultado.despesas)
    resultado.despesas = [
        d for d in resultado.despesas
        if (c := motor.competencia_da_data(d.data_corrigida or d.carimbo.date(), competencias)) is not None
        and c.pk == imp.competencia_id]
    if not resultado.despesas:
        return _falhar(imp, f"Nenhuma das {total_planilha} respostas da planilha é do período {imp.competencia} "
                            f"({imp.competencia.periodo_ini:%d/%m/%Y} a {imp.competencia.fechamento:%d/%m/%Y}).")

    with transaction.atomic():
        FormDespesa.objects.bulk_create([_form(imp, rep, d) for d in resultado.despesas], batch_size=1000)
        anterior = Importacao.objects.select_for_update().filter(
            tipo=Importacao.Tipo.PLANILHA, representante=rep, competencia=imp.competencia, ativo=True
        ).exclude(pk=imp.pk).first()
        if anterior:
            anterior.ativo = False
            anterior.save(update_fields=["ativo"])
        imp.status = Importacao.Status.OK
        imp.ativo = True
        imp.ativado_em = timezone.now()
        imp.resumo = resultado.contagens() | {"versao_anterior": anterior.versao if anterior else None,
                                              "respostas_na_planilha": total_planilha,
                                              "outros_periodos": total_planilha - len(resultado.despesas)}
        imp.save()

    auditoria.registrar(None, "IMPORTACAO", "OK", representante=rep, importacao=imp, usuario=imp.usuario, ip=imp.ip,
                        detalhe={k: v for k, v in imp.resumo.items() if k != "colunas_descartadas"})
    log.info("planilha importada", extra={"importacao_id": imp.pk, "representante_id": rep.pk,
                                          "linhas": imp.resumo["linhas"]})
    cartoes = list(Cartao.objects.filter(representante=rep, escopo=Cartao.Escopo.CONCILIAR_FORMULARIO))
    if cartoes:
        execucoes = motor.executar_abertas(cartoes, motivo="planilha atualizada", usuario=imp.usuario)
        imp.resumo = imp.resumo | {"execucoes": [e.pk for e in execucoes]}
        imp.save(update_fields=["resumo"])
    return imp


def _form(imp, rep, d) -> FormDespesa:
    return FormDespesa(importacao=imp, representante=rep, linha_planilha=d.linha_planilha, carimbo=d.carimbo,
                       data_informada=d.data_informada, data_corrigida=d.data_corrigida, despesa=d.despesa[:60],
                       descricao=d.descricao, finalidade=d.finalidade, pagamento=d.pagamento[:60],
                       medico_original=d.medico_original[:200], medico_canonico=d.medico_canonico[:200],
                       paciente=d.paciente[:200], cidade=d.cidade[:120], valor=d.valor,
                       valor_original=d.valor_original, link_comprovante=d.link_comprovante,
                       data_suspeita=d.data_suspeita, duplicado=d.duplicado, valor_invalido=d.valor_invalido,
                       pagamento_outro_cartao=d.pagamento_outro_cartao, moeda_estrangeira=d.moeda_estrangeira)


def importar_geral(origem, nome_original: str, competencias, *, usuario="", ip=None, maquina="") -> dict:
    """Planilha GERAL REPRESENTANTES (todos os representantes): para cada competência escolhida, uma versão de
    planilha por representante com as linhas daquele período (a versão anterior fica inativa; nada é apagado).
    Quem tem cartão conciliado por formulário e aparece na planilha recebe a versão mesmo sem linhas no período
    (assim o que estiver na fatura vira "sem formulário", e não "aguardando planilha").
    Representante que não existe no cadastro é criado (como na análise). Devolve o resumo por competência."""
    from ..analise import _representantes
    competencias = [c for c in competencias if not c.fechada]
    hash_arquivo, caminho = arquivos.guardar(origem, nome_original, subpasta="planilhas", extensoes={".xlsx"},
                                             assinatura=ASSINATURA_XLSX, descricao="uma planilha .xlsx")
    params = parametros.obter_todos()
    por_nome = normalizar_geral(caminho, params)
    obter_rep, criados = _representantes(params["de_para_representantes"])
    por_rep = {}
    for nome, resultado in por_nome.items():
        por_rep.setdefault(obter_rep(nome), []).extend(resultado.despesas)
    resumo = {"criados": criados, "competencias": {}}
    for comp in sorted(competencias, key=lambda c: c.periodo_ini):
        todas = list(Competencia.objects.filter(conta_id=comp.conta_id).order_by("periodo_ini"))
        conciliam = set(Cartao.objects.filter(conta_id=comp.conta_id, escopo=Cartao.Escopo.CONCILIAR_FORMULARIO)
                        .values_list("representante_id", flat=True))
        linhas_comp = {}
        for rep, despesas in por_rep.items():
            desta = [d for d in despesas
                     if (c := motor.competencia_da_data(d.data_corrigida or d.carimbo.date(), todas)) is not None
                     and c.pk == comp.pk]
            if not desta and rep.pk not in conciliam:
                continue
            with transaction.atomic():
                Representante.objects.select_for_update().get(pk=rep.pk)
                anterior = Importacao.objects.filter(tipo=Importacao.Tipo.PLANILHA, representante=rep,
                                                     competencia=comp, ativo=True).first()
                ultima = Importacao.objects.filter(tipo=Importacao.Tipo.PLANILHA, representante=rep,
                                                   competencia=comp).aggregate(v=Max("versao"))["v"] or 0
                if anterior:
                    anterior.ativo = False
                    anterior.save(update_fields=["ativo"])
                contagens = Resultado(despesas=desta).contagens()
                imp = Importacao.objects.create(
                    tipo=Importacao.Tipo.PLANILHA, representante=rep, competencia=comp, versao=ultima + 1,
                    origem=Importacao.Origem.UPLOAD, status=Importacao.Status.OK, ativo=True,
                    ativado_em=timezone.now(), arquivo_caminho=str(caminho), arquivo_nome_original=nome_original[:255],
                    hash_sha256=hash_arquivo, usuario=usuario, ip=ip, maquina=maquina,
                    resumo=contagens | {"planilha_geral": True, "respostas_na_planilha": len(despesas),
                                        "outros_periodos": len(despesas) - len(desta),
                                        "versao_anterior": anterior.versao if anterior else None})
                FormDespesa.objects.bulk_create([_form(imp, rep, d) for d in desta], batch_size=1000)
            auditoria.registrar(None, "IMPORTACAO", "OK", representante=rep, competencia=comp, importacao=imp,
                                usuario=usuario, ip=ip, detalhe={"planilha_geral": True, "linhas": len(desta)})
            linhas_comp[rep.nome] = len(desta)
        resumo["competencias"][str(comp)] = linhas_comp
    motor.executar_abertas(motivo="planilha geral", usuario=usuario)
    log.info("planilha geral importada", extra={"competencias": len(competencias)})
    return resumo


def _falhar(imp, mensagem):
    imp.status = Importacao.Status.ERRO
    imp.resumo = {"erro": mensagem}
    imp.save(update_fields=["status", "resumo"])
    auditoria.registrar(None, "IMPORTACAO", "ERRO", representante=imp.representante, importacao=imp,
                        usuario=imp.usuario, ip=imp.ip, detalhe={"erro": mensagem})
    return imp


def importar_geral_por_ids(caminho, nome_original, competencia_ids, usuario="", ip=None):
    """Para a fila (RQ): recebe ids, não objetos."""
    competencias = list(Competencia.objects.filter(pk__in=competencia_ids))
    try:
        return importar_geral(caminho, nome_original, competencias, usuario=usuario, ip=ip)
    except PlanilhaInvalida as exc:
        auditoria.registrar(None, "IMPORTACAO", "ERRO", usuario=usuario, ip=ip,
                            detalhe={"planilha_geral": True, "erro": str(exc)})
        raise
