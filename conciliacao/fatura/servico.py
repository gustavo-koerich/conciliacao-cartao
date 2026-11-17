"""Importação da fatura: recebe o arquivo, lê (texto ou OCR), confere e grava, numa transação.

Fluxo:
1. criar_importacao(): valida e guarda o arquivo (nome = SHA-256), cria a importação PROCESSANDO.
   Mesmo arquivo da versão ativa: não cria nada.
2. processar(): roda em segundo plano (RQ). Lê, confere os totais e grava competência, seções e lançamentos.
   A versão só fica ativa se a trava fechar; se já houver uma versão ativa, a nova fica aguardando a confirmação
   da pessoa (prévia "em uso × novo", etapa 8).
"""
import logging
from collections import Counter
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .. import arquivos, auditoria, motor
from ..arquivos import ArquivoRecusado  # noqa: F401 (usado pelas views e comandos)
from ..models import Cartao, Competencia, ContaCartao, FaturaLanc, FaturaSecao, Importacao, conta_padrao
from . import bb
from .interpretacao import ErroCabecalho, cabecalho_parcial, interpretar, ler_cabecalho
from .leitura import ErroLeitura, ler_pdf

log = logging.getLogger(__name__)

MAX_PROBLEMAS_NO_RESUMO = 200


# Formatos aceitos por leitor de banco: extensão → (assinatura do arquivo, descrição para a mensagem de erro)
FORMATOS = {
    ContaCartao.Leitor.AILOS_PDF: {".pdf": b"%PDF-"},
    ContaCartao.Leitor.BB: {".pdf": b"%PDF-", ".xlsx": b"PK"},
}


def _validar_e_guardar(origem, nome_original, conta):
    formatos = FORMATOS[conta.leitor]
    descricao = " ou ".join({".pdf": "um PDF", ".xlsx": "a planilha .xlsx exportada do banco"}[e] for e in formatos)
    return arquivos.guardar(origem, nome_original, subpasta="faturas", extensoes=set(formatos),
                            assinatura=tuple(formatos.values()), descricao=descricao)


def criar_importacao(origem, nome_original: str, *, conta=None, usuario="", ip=None, maquina="",
                     tipo_origem=Importacao.Origem.UPLOAD) -> tuple[Importacao, bool]:
    """Devolve (importação, criada). criada=False quando o arquivo é o mesmo da versão ativa."""
    conta = conta or ContaCartao.objects.get(pk=conta_padrao())
    hash_arquivo, caminho = _validar_e_guardar(origem, nome_original, conta)
    ativa = Importacao.objects.filter(tipo=Importacao.Tipo.FATURA, ativo=True, hash_sha256=hash_arquivo).first()
    if ativa:
        auditoria.registrar(None, "IMPORTACAO", "RECUSADO", competencia=ativa.competencia, importacao=ativa,
                            usuario=usuario, ip=ip, detalhe={"motivo": "mesmo arquivo da versão ativa"})
        return ativa, False
    imp = Importacao.objects.create(
        tipo=Importacao.Tipo.FATURA, conta=conta, origem=tipo_origem, status=Importacao.Status.PROCESSANDO,
        arquivo_caminho=str(caminho), arquivo_nome_original=nome_original[:255], hash_sha256=hash_arquivo,
        usuario=usuario, ip=ip, maquina=maquina,
        resumo={"progresso": {"feito": 0, "total": 1, "etapa": "na fila"}},
    )
    return imp, True


def reler(imp: Importacao, *, usuario="", ip=None) -> Importacao:
    """Lê de novo o MESMO arquivo com o leitor atual (ex.: depois de uma correção no leitor), como versão nova.
    Se fechar e a competência já tiver versão em uso, a nova fica aguardando "Usar esta versão"."""
    if imp.tipo != Importacao.Tipo.FATURA or not imp.arquivo_caminho:
        raise AtivacaoRecusada("Só faturas podem ser lidas de novo.")
    nova = Importacao.objects.create(
        tipo=Importacao.Tipo.FATURA, conta=imp.conta, origem=imp.origem, status=Importacao.Status.PROCESSANDO,
        arquivo_caminho=imp.arquivo_caminho, arquivo_nome_original=imp.arquivo_nome_original,
        hash_sha256=imp.hash_sha256, usuario=usuario, ip=ip,
        resumo={"progresso": {"feito": 0, "total": 1, "etapa": "na fila"}, "relida_de": imp.pk})
    auditoria.registrar(None, "REPROCESSAMENTO", "OK", competencia=imp.competencia, importacao=nova, usuario=usuario,
                        ip=ip, detalhe={"relida_de": imp.pk})
    return nova


def processar(importacao_id: int, forcar_ocr: bool = False) -> Importacao:
    """Tarefa de fundo: lê, confere e grava. Nunca deixa a importação em PROCESSANDO."""
    imp = Importacao.objects.select_related("conta").get(pk=importacao_id)
    if imp.status != Importacao.Status.PROCESSANDO:
        return imp
    conta = imp.conta or ContaCartao.objects.get(pk=conta_padrao())
    # Resumo conferido pela pessoa depois de uma leitura que falhou (fica guardado em todas as etapas).
    informado = (imp.resumo or {}).get("cabecalho_informado")
    extra = {"cabecalho_informado": informado} if informado else {}

    def progresso(feito, total, etapa):
        Importacao.objects.filter(pk=imp.pk).update(
            resumo={"progresso": {"feito": feito, "total": total, "etapa": etapa}} | extra)

    try:
        fatura = _ler(conta, imp.arquivo_caminho, progresso, forcar_ocr, _cabecalho_de(informado))
    except ErroCabecalho as exc:
        return _falhar(imp, str(exc), {"cabecalho_parcial": _serializar(exc.parcial),
                                       "trecho_lido": exc.trecho[:3000]} | extra)
    except ErroLeitura as exc:
        return _falhar(imp, str(exc), extra)
    except Exception:  # noqa: BLE001 - qualquer falha precisa virar ERRO visível, não PROCESSANDO eterno
        log.exception("falha inesperada na leitura da fatura", extra={"importacao_id": imp.pk})
        return _falhar(imp, "Falha inesperada na leitura. Detalhes no log da aplicação.")
    imp = _gravar(imp, fatura)
    if imp.ativo:
        # Notas fiscais já enviadas podem cobrir lançamentos desta fatura (ex.: parcelas seguintes).
        from .. import notas
        notas.casar(imp.competencia, usuario="automático")
        # Nova fatura em uso: reconcilia todos os cartões das competências abertas.
        execucoes = motor.executar_abertas(motivo="fatura importada", usuario=imp.usuario)
        imp.resumo = imp.resumo | {"execucoes": [e.pk for e in execucoes]}
        imp.save(update_fields=["resumo"])
    return imp


def _ler(conta, caminho, progresso, forcar_ocr, informado=None):
    """Escolhe o leitor pelo banco da conta e pelo formato do arquivo; todos devolvem a mesma FaturaLida.
    informado: resumo conferido pela pessoa (substitui a leitura do cabeçalho; a trava de totais continua valendo)."""
    if conta.leitor == ContaCartao.Leitor.BB:
        if str(caminho).lower().endswith(".xlsx"):
            fatura = bb.ler_xlsx(caminho)
        else:
            fatura = bb.ler_pdf(caminho, informado=informado)
    else:
        bruta = ler_pdf(caminho, progresso=progresso, forcar_ocr=forcar_ocr)
        if informado:
            cabecalho = informado
        else:
            try:
                cabecalho = ler_cabecalho(bruta.texto_cabecalho)
            except ErroCabecalho as exc:
                exc.parcial = cabecalho_parcial(bruta.texto_cabecalho)
                exc.trecho = bruta.texto_cabecalho
                raise
        fatura = interpretar(bruta, cabecalho)
    # Início do período: o dia seguinte ao fechamento anterior da mesma conta, quando o banco não informa.
    anterior = (Competencia.objects.filter(conta=conta, fechamento__lt=fatura.fechamento)
                .order_by("-fechamento").first())
    if conta.leitor == ContaCartao.Leitor.BB and anterior:
        fatura.periodo_ini = anterior.fechamento + timedelta(days=1)
        if fatura.conta:
            for l in fatura.conta.lancamentos:
                if l.natureza == "SALDO_ANTERIOR":
                    l.data = fatura.periodo_ini
    return fatura


class AtivacaoRecusada(Exception):
    pass


def pode_ignorar_divergencia(imp: Importacao) -> bool:
    """A leitura terminou e gravou os lançamentos, mas a trava de totais não fechou: a pessoa pode decidir que a
    diferença não é relevante e seguir (fica registrado quem, quando e por quê)."""
    return (imp.tipo == Importacao.Tipo.FATURA and imp.status == Importacao.Status.ERRO and imp.competencia_id
            and imp.versao and not imp.resumo.get("erro") and imp.lancamentos.exists()
            and not imp.competencia.fechada)


def pode_usar_versao(imp: Importacao) -> bool:
    return (imp.tipo == Importacao.Tipo.FATURA and imp.status == Importacao.Status.OK and not imp.ativo
            and imp.competencia_id and not imp.competencia.fechada)


def _ativar(imp: Importacao, *, usuario, ip, acao, detalhe):
    """Coloca esta versão em uso no lugar da atual e refaz a conciliação."""
    with transaction.atomic():
        comp = Competencia.objects.select_for_update().get(pk=imp.competencia_id)
        if comp.fechada:
            raise AtivacaoRecusada(f"A competência {comp} está FECHADA.")
        Importacao.objects.filter(tipo=Importacao.Tipo.FATURA, competencia=comp, ativo=True).update(ativo=False)
        imp.status, imp.ativo, imp.ativado_em = Importacao.Status.OK, True, timezone.now()
        imp.save(update_fields=["status", "ativo", "ativado_em", "resumo"])
        r = imp.resumo
        comp.periodo_ini = r["periodo"][0]
        comp.periodo_fim = r["periodo"][1]
        comp.fechamento, comp.vencimento = r["fechamento"], r["vencimento"]
        comp.total_fatura = Decimal(r["total_fatura"])
        comp.save()
        auditoria.registrar(None, acao, "OK", competencia=comp, importacao=imp, usuario=usuario, ip=ip,
                            detalhe=detalhe | {"versao": imp.versao})
    from .. import notas
    notas.casar(imp.competencia, usuario=usuario or "automático")
    motor.executar_abertas(motivo="fatura em uso", usuario=usuario)
    return imp


def ignorar_divergencia(imp: Importacao, justificativa: str, *, usuario="", ip=None) -> Importacao:
    if not pode_ignorar_divergencia(imp):
        raise AtivacaoRecusada("Esta importação não pode seguir com divergência.")
    justificativa = " ".join((justificativa or "").split())[:500]
    if len(justificativa) < 5:
        raise AtivacaoRecusada("Escreva o motivo para seguir mesmo com a diferença.")
    imp.resumo = imp.resumo | {"divergencia_aceita": {"usuario": usuario, "em": timezone.now().isoformat(),
                                                      "justificativa": justificativa,
                                                      "diferenca": imp.resumo.get("diferenca_geral")}}
    return _ativar(imp, usuario=usuario, ip=ip, acao="ATUALIZACAO",
                   detalhe={"divergencia_aceita": justificativa, "diferenca": imp.resumo.get("diferenca_geral")})


def usar_versao(imp: Importacao, *, usuario="", ip=None) -> Importacao:
    if not pode_usar_versao(imp):
        raise AtivacaoRecusada("Esta versão não pode ser colocada em uso.")
    imp.resumo = {k: v for k, v in imp.resumo.items() if k not in ("aguardando_confirmacao", "versao_ativa")}
    return _ativar(imp, usuario=usuario, ip=ip, acao="ATUALIZACAO", detalhe={"substituiu_versao": True})


def relatorio_suporte(imp: Importacao) -> str:
    """Texto para mandar ao suporte: o que foi enviado, o que deu errado e onde. Sem dados de formulário/paciente."""
    from django.conf import settings
    r = imp.resumo or {}
    linhas = [
        "RELATÓRIO PARA O SUPORTE — Conciliação de cartão (Empresa Demo)",
        f"Versão do sistema: {getattr(settings, 'APP_VERSAO', '?')}",
        f"Gerado em: {timezone.localtime():%d/%m/%Y %H:%M}",
        f"Importação nº {imp.pk} · banco {imp.conta or '—'} · competência {imp.competencia or '—'} · versão {imp.versao or '—'}",
        f"Arquivo: {imp.arquivo_nome_original} (SHA-256 {imp.hash_sha256})",
        f"Enviado em {timezone.localtime(imp.criado_em):%d/%m/%Y %H:%M} por {imp.usuario or '—'} ({imp.ip or '—'})",
        f"Situação: {imp.get_status_display()} · leitura: {imp.get_metodo_leitura_display() or '—'}",
    ]
    if r.get("erro"):
        linhas.append(f"Erro: {r['erro']}")
    if r.get("cabecalho_parcial") is not None:
        linhas.append(f"Resumo que deu para ler: {r['cabecalho_parcial'] or 'nada'}")
    if r.get("cabecalho_informado"):
        linhas.append(f"Resumo informado pela pessoa: {r['cabecalho_informado']}")
    if r.get("trecho_lido"):
        linhas.append("Texto do resumo como foi lido (OCR/PDF):")
        linhas += [f"  | {l}" for l in r["trecho_lido"].splitlines()[:60]]
    if "total_fatura" in r:
        linhas.append(f"Total da fatura {r.get('total_fatura')} · soma lida {r.get('soma_lida')} · "
                      f"diferença {r.get('diferenca_geral')} · portadores que fecharam {r.get('portadores_fechados')} de {r.get('portadores')}")
    for s in imp.secoes.exclude(status=FaturaSecao.Status.OK).select_related("cartao"):
        linhas.append(f"Seção com diferença: cartão {s.cartao.final if s.cartao else 'conta'} {s.nome_portador} — "
                      f"impresso {s.total_declarado} · lido {s.total_lido} · diferença {s.diferenca} · pág. {s.pagina_ini}-{s.pagina_fim}")
    for e in r.get("erros", []):
        linhas.append(f"Erro na leitura: {e}")
    if r.get("problemas"):
        linhas.append(f"Linhas que não viraram lançamento ({len(r['problemas'])}):")
        linhas += [f"  - {p}" for p in r["problemas"][:50]]
    return "\n".join(linhas)


CAMPOS_CABECALHO = ("periodo_ini", "periodo_fim", "fechamento", "vencimento", "total")


def _serializar(cab: dict) -> dict:
    return {k: (str(v) if isinstance(v, Decimal) else v.isoformat()) for k, v in (cab or {}).items() if v is not None}


def _cabecalho_de(informado):
    if not informado:
        return None
    from datetime import date
    return {k: (Decimal(v) if k == "total" else date.fromisoformat(v)) for k, v in informado.items()
            if k in CAMPOS_CABECALHO}


def pode_informar_cabecalho(imp: Importacao) -> bool:
    return (imp.tipo == Importacao.Tipo.FATURA and imp.status == Importacao.Status.ERRO
            and "cabecalho_parcial" in (imp.resumo or {}))


def campos_cabecalho(imp: Importacao) -> tuple:
    """BB: o início do período vem do fechamento anterior; só fechamento, vencimento e total."""
    conta = imp.conta or ContaCartao.objects.get(pk=conta_padrao())
    return CAMPOS_CABECALHO[2:] if conta.leitor == ContaCartao.Leitor.BB else CAMPOS_CABECALHO


def informar_cabecalho(imp: Importacao, dados: dict, *, usuario="", ip=None) -> Importacao:
    """A leitura do resumo falhou e a pessoa disse que não é relevante: confere/corrige os dados e a leitura
    recomeça com eles. Os lançamentos continuam sendo lidos do arquivo e a trava de totais continua valendo."""
    if not pode_informar_cabecalho(imp):
        raise AtivacaoRecusada("Esta importação não está esperando os dados do resumo.")
    from datetime import date
    valores = {}
    try:
        for campo in campos_cabecalho(imp):
            bruto = (dados.get(campo) or "").strip()
            valores[campo] = (Decimal(bruto.replace(".", "").replace(",", ".")) if campo == "total" and "," in bruto
                              else Decimal(bruto) if campo == "total" else date.fromisoformat(bruto))
    except Exception as exc:  # noqa: BLE001
        raise AtivacaoRecusada("Preencha todos os campos (datas e total da fatura).") from exc
    if "periodo_ini" in valores and valores["periodo_ini"] >= valores["periodo_fim"]:
        raise AtivacaoRecusada("O início do período precisa ser antes do fim.")
    if not 0 < (valores["vencimento"] - valores["fechamento"]).days <= 40:
        raise AtivacaoRecusada("O vencimento precisa ser até 40 dias depois do fechamento.")
    if valores["total"] <= 0:
        raise AtivacaoRecusada("Informe o total da fatura.")
    informado = _serializar(valores) | {"usuario": usuario, "em": timezone.now().isoformat(),
                                        "erro_original": imp.resumo.get("erro", "")}
    imp.status = Importacao.Status.PROCESSANDO
    imp.resumo = {"cabecalho_informado": informado, "progresso": {"feito": 0, "total": 1, "etapa": "na fila"}}
    imp.save(update_fields=["status", "resumo"])
    auditoria.registrar(None, "REPROCESSAMENTO", "OK", importacao=imp, usuario=usuario, ip=ip,
                        detalhe={"cabecalho_informado": _serializar(valores)})
    from .. import tarefas
    tarefas.enfileirar_fatura(imp.pk)
    return imp


def _falhar(imp: Importacao, mensagem: str, extra=None) -> Importacao:
    imp.status = Importacao.Status.ERRO
    imp.resumo = {"erro": mensagem} | (extra or {})
    imp.save(update_fields=["status", "resumo"])
    auditoria.registrar(None, "IMPORTACAO", "ERRO", importacao=imp, usuario=imp.usuario, ip=imp.ip,
                        detalhe={"erro": mensagem})
    return imp


def _decimal(v):
    return None if v is None else str(v)


@transaction.atomic
def _gravar(imp: Importacao, fat) -> Importacao:
    ano_mes = f"{fat.fechamento:%Y-%m}"
    conta = imp.conta or ContaCartao.objects.get(pk=conta_padrao())
    competencia, _ = Competencia.objects.select_for_update().get_or_create(conta=conta, ano_mes=ano_mes, defaults={
        "periodo_ini": fat.periodo_ini, "periodo_fim": fat.periodo_fim, "fechamento": fat.fechamento,
        "vencimento": fat.vencimento, "total_fatura": fat.total_cabecalho})
    if competencia.fechada:
        imp.competencia = competencia
        imp.save(update_fields=["competencia"])
        return _falhar(imp, f"A competência {competencia} está FECHADA e não aceita nova fatura.")

    ultima = Importacao.objects.filter(tipo=Importacao.Tipo.FATURA, competencia=competencia).aggregate(v=Max("versao"))
    imp.competencia = competencia
    imp.versao = (ultima["v"] or 0) + 1
    imp.metodo_leitura = fat.metodo

    # Cartões: a chave é o final. Final desconhecido vira cadastro pendente (aviso na tela).
    cartoes, novos = {}, []
    for secao in fat.portadores:
        cartao, criado = Cartao.objects.get_or_create(conta=conta, final=secao.final, defaults={
            "nome_fatura": secao.nome[:120], "pendente_cadastro": True})
        cartoes[secao.final] = cartao
        if criado:
            novos.append(secao.final)

    for secao in fat.secoes:
        cartao = cartoes.get(secao.final)
        FaturaSecao.objects.create(
            importacao=imp, cartao=cartao, nome_portador=secao.nome[:120], total_declarado=secao.total_declarado,
            total_lido=secao.soma, diferenca=secao.diferenca, pagina_ini=secao.pagina_ini,
            pagina_fim=secao.pagina_fim or secao.pagina_ini, metodo_leitura=fat.metodo,
            status=FaturaSecao.Status.OK if secao.ok else FaturaSecao.Status.ERRO_LEITURA)

    ocorrencias = Counter()
    linhas = []
    for secao in fat.secoes:
        cartao = cartoes.get(secao.final)
        for l in secao.lancamentos:
            chave = (secao.final, l.data, l.descricao[:200], l.valor)
            ocorrencias[chave] += 1
            linhas.append(FaturaLanc(
                importacao=imp, cartao=cartao, secao=l.secao, natureza=l.natureza, data=l.data,
                descricao=l.descricao[:200], cidade=l.cidade[:80], valor=l.valor, parcela_atual=l.parcela_atual,
                parcela_total=l.parcela_total, moeda=l.moeda, valor_moeda=l.valor_moeda, cotacao=l.cotacao,
                ordem=l.ordem, pagina=l.pagina, n_ocorrencia=ocorrencias[chave]))
    FaturaLanc.objects.bulk_create(linhas, batch_size=1000)

    fechou, total_portadores = fat.taxa_fechamento()
    resumo = {
        "periodo": [fat.periodo_ini.isoformat(), fat.periodo_fim.isoformat()],
        "fechamento": fat.fechamento.isoformat(),
        "fechamento_estimado": bool(getattr(fat, "fechamento_estimado", False)),
        "conta": conta.banco,
        "vencimento": fat.vencimento.isoformat(),
        "total_fatura": _decimal(fat.total_cabecalho),
        "soma_lida": _decimal(fat.soma_geral),
        "diferenca_geral": _decimal(fat.soma_geral - fat.total_cabecalho),
        "encargos_conta": _decimal(fat.encargos),
        "saldo_anterior": _decimal(sum((l.valor for l in fat.lancamentos if l.natureza == "SALDO_ANTERIOR"),
                                       Decimal("0"))),
        "pagamentos": _decimal(sum((l.valor for l in fat.lancamentos if l.natureza == "PAGAMENTO"), Decimal("0"))),
        "lancamentos": len(linhas),
        "portadores_fechados": fechou,
        "portadores": total_portadores,
        "cartoes_novos": novos,
        "erros": fat.erros,
        **({"cabecalho_informado": (imp.resumo or {})["cabecalho_informado"]}
           if (imp.resumo or {}).get("cabecalho_informado") else {}),
        "problemas": ([p for s in fat.secoes for p in s.problemas] + fat.problemas)[:MAX_PROBLEMAS_NO_RESUMO],
    }
    imp.status = Importacao.Status.OK if fat.fecha else Importacao.Status.ERRO
    ativa = Importacao.objects.select_for_update().filter(
        tipo=Importacao.Tipo.FATURA, competencia=competencia, ativo=True).first()
    if imp.status == Importacao.Status.OK and ativa is None:
        imp.ativo = True
        imp.ativado_em = timezone.now()
        for campo in ("periodo_ini", "periodo_fim", "fechamento", "vencimento"):
            setattr(competencia, campo, getattr(fat, campo))
        competencia.total_fatura = fat.total_cabecalho
        competencia.save()
    elif imp.status == Importacao.Status.OK:
        resumo["aguardando_confirmacao"] = True
        resumo["versao_ativa"] = ativa.versao
    imp.resumo = resumo
    imp.save()

    auditoria.registrar(None, "IMPORTACAO", "OK" if imp.status == Importacao.Status.OK else "ERRO",
                        competencia=competencia, importacao=imp, usuario=imp.usuario, ip=imp.ip,
                        detalhe={k: resumo[k] for k in ("total_fatura", "soma_lida", "portadores_fechados",
                                                        "portadores", "lancamentos", "cartoes_novos")}
                        | {"versao": imp.versao, "ativa": imp.ativo, "metodo": fat.metodo})
    log.info("fatura importada", extra={"importacao_id": imp.pk, "competencia": ano_mes, "versao": imp.versao,
                                        "status": imp.status, "metodo": fat.metodo,
                                        "portadores_fechados": f"{fechou}/{total_portadores}"})
    return imp
