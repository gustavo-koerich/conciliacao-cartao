"""Portal de pendências: o representante responde, pelo celular, os gastos da fatura que estão sem formulário.

Fluxo: o financeiro gera um link pessoal (validade escolhida) → envia pelo WhatsApp → o representante abre, preenche
categoria/descrição/médico/paciente e tira a foto do comprovante → a linha fica laranja ("respondido, aguardando
aprovação") → o financeiro aprova (verde) ou recusa (volta a pendente com o motivo; o representante pode responder
de novo pelo mesmo link, enquanto valer).

Segurança: o código do link tem 256 bits aleatórios e só a impressão (SHA-256) fica no banco; o link expira, pode ser
revogado e só mostra os lançamentos daquele representante listados na hora de gerar. Pela internet o portal fica numa
porta própria (Tailscale Funnel) que só responde a /pendencias/. Nada de dados de paciente em log.
"""
import hashlib
import logging
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import arquivos, auditoria, motor, parametros
from .models import (Anexo, Auditoria, Cartao, FaturaLanc, Importacao, LinkPendencias, Representante, RespostaPendencia,
                     Resultado, chave_lancamento)

log = logging.getLogger(__name__)
S = Resultado.Status

PENDENTES = (S.SEM_FORMULARIO, S.AGUARDANDO_PLANILHA, S.AGUARDANDO_APROVACAO)
VALIDADES = (1, 3, 7, 15, 30)                       # dias, para escolher ao gerar
TIPOS_COMPROVANTE = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                     ".webp": "image/webp", ".heic": "image/heic"}
ASSINATURAS_COMPROVANTE = (b"%PDF-", b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF", b"\x00\x00\x00")


class PendenciaInvalida(Exception):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def url_do_link(token: str) -> str:
    base = (settings.PORTAL_URL_BASE or "").rstrip("/")
    return f"{base}/pendencias/{token}/"


def lancamentos_pendentes(rep: Representante, competencia):
    return (Resultado.objects.filter(competencia=competencia, vigente=True, representante=rep, status__in=PENDENTES,
                                     fatura_lanc__isnull=False, cartao__escopo=Cartao.Escopo.CONCILIAR_FORMULARIO)
            .select_related("fatura_lanc").order_by("fatura_lanc__data", "fatura_lanc__ordem"))


def gerar_link(rep: Representante, competencia, dias: int, *, usuario="", ip=None) -> tuple[LinkPendencias, str]:
    if dias not in VALIDADES:
        raise PendenciaInvalida("Validade inválida.")
    if competencia.fechada:
        raise PendenciaInvalida(f"A competência {competencia} está fechada.")
    chaves = [chave_lancamento(r.fatura_lanc) for r in lancamentos_pendentes(rep, competencia)]
    if not chaves:
        raise PendenciaInvalida("Este representante não tem gastos sem formulário nesta fatura.")
    token = secrets.token_urlsafe(32)
    link = LinkPendencias.objects.create(representante=rep, competencia=competencia, token_hash=_hash(token),
                                         chaves=chaves, criado_por=usuario,
                                         expira_em=timezone.now() + timedelta(days=dias))
    auditoria.registrar(None, "CADASTRO", "OK", competencia=competencia, representante=rep, usuario=usuario, ip=ip,
                        detalhe={"link_pendencias": link.pk, "itens": len(chaves), "dias": dias})
    return link, token


def mensagem(link: LinkPendencias, token: str) -> str:
    primeiro = link.representante.nome.split()[0]
    return (f"Olá, {primeiro}! Há {len(link.chaves)} gasto(s) do cartão sem formulário na fatura "
            f"{link.competencia}. Por favor, responda e envie o comprovante por este link "
            f"(válido até {timezone.localtime(link.expira_em):%d/%m/%Y}): {url_do_link(token)}")


def link_whatsapp(texto: str) -> str:
    return "https://wa.me/?text=" + quote(texto)


def buscar(token: str) -> LinkPendencias | None:
    if not token or len(token) > 100:
        return None
    return (LinkPendencias.objects.select_related("representante", "competencia__conta")
            .filter(token_hash=_hash(token)).first())


def registrar_acesso(link: LinkPendencias):
    agora = timezone.now()
    LinkPendencias.objects.filter(pk=link.pk).update(acessos=link.acessos + 1, ultimo_acesso_em=agora,
                                                     aberto_em=link.aberto_em or agora)


def itens(link: LinkPendencias) -> list[dict]:
    """Os lançamentos do link, com a resposta (se houver) e se ainda dá para editar."""
    fatura = link.competencia.importacoes.filter(tipo=Importacao.Tipo.FATURA, ativo=True).first()
    if fatura is None:
        return []
    lancs = {chave_lancamento(l): l for l in FaturaLanc.objects.filter(
        importacao=fatura, cartao__representante=link.representante)}
    respostas = {r.chave: r for r in RespostaPendencia.objects.filter(chave__in=link.chaves, ativo=True)}
    resultados = {chave_lancamento(r.fatura_lanc): r for r in Resultado.objects.filter(
        competencia=link.competencia, vigente=True, fatura_lanc__importacao=fatura,
        fatura_lanc__cartao__representante=link.representante).select_related("fatura_lanc")}
    saida = []
    for n, chave in enumerate(link.chaves, start=1):
        lanc = lancs.get(chave)
        if lanc is None:
            continue
        resp, res = respostas.get(chave), resultados.get(chave)
        conciliado_por_formulario = res is not None and res.form_despesa_id is not None
        saida.append({"n": n, "chave": chave, "lanc": lanc, "resposta": resp,
                      "editavel": not conciliado_por_formulario and not (resp and resp.aprovado_em),
                      "situacao": ("conciliado" if conciliado_por_formulario or (resp and resp.aprovado_em)
                                   else "recusado" if resp and resp.recusado_em
                                   else "enviado" if resp else "pendente")})
    return saida


def responder(link: LinkPendencias, n: int, dados: dict, arquivo=None, *, ip=None) -> RespostaPendencia:
    if not link.valido:
        raise PendenciaInvalida("Este link não vale mais. Fale com o financeiro.")
    item = next((i for i in itens(link) if i["n"] == n), None)
    if item is None:
        raise PendenciaInvalida("Item não encontrado.")
    if not item["editavel"]:
        raise PendenciaInvalida("Este gasto já foi conferido pelo financeiro.")
    nao_reconhece = dados.get("nao_reconhece") in ("1", "on", True)
    categoria = (dados.get("categoria") or "").strip()
    if not nao_reconhece and categoria not in parametros.CATEGORIAS_DESPESA:
        raise PendenciaInvalida("Escolha a categoria do gasto.")
    if not nao_reconhece and not (dados.get("descricao") or "").strip():
        raise PendenciaInvalida("Descreva o gasto.")
    texto = lambda c, n=200: " ".join(str(dados.get(c) or "").split())[:n]  # noqa: E731
    with transaction.atomic():
        resp = RespostaPendencia.objects.filter(chave=item["chave"], ativo=True).first() or RespostaPendencia(
            link=link, chave=item["chave"])
        resp.link = link
        resp.categoria = "" if nao_reconhece else categoria
        resp.descricao, resp.medico, resp.paciente = texto("descricao", 1000), texto("medico"), texto("paciente")
        resp.cidade, resp.observacao = texto("cidade", 120), texto("observacao", 1000)
        resp.nao_reconhece, resp.ip = nao_reconhece, ip
        resp.recusado_em, resp.recusado_por, resp.motivo_recusa = None, "", ""      # respondeu de novo
        resp.save()
        if arquivo is not None:
            hash_arquivo, caminho = arquivos.guardar(
                arquivo, arquivo.name, subpasta="comprovantes", extensoes=set(TIPOS_COMPROVANTE),
                assinatura=ASSINATURAS_COMPROVANTE, descricao="uma foto (JPG, PNG, WEBP, HEIC) ou PDF")
            extensao = "." + arquivo.name.rsplit(".", 1)[-1].lower() if "." in arquivo.name else ".jpg"
            Anexo.objects.create(competencia=link.competencia, chave=item["chave"], arquivo_caminho=str(caminho),
                                 nome_original=arquivo.name[:255], mime=TIPOS_COMPROVANTE.get(extensao, "image/jpeg"),
                                 hash_sha256=hash_arquivo, tamanho=arquivo.size,
                                 usuario=f"representante: {link.representante.nome} (link)", ip=ip)
    auditoria.registrar(None, "ANEXO" if arquivo else "AJUSTE_MANUAL", "OK", competencia=link.competencia,
                        representante=link.representante, usuario=f"representante: {link.representante.nome}", ip=ip,
                        detalhe={"resposta_pendencia": resp.pk, "link": link.pk, "comprovante": arquivo is not None,
                                 "nao_reconhece": nao_reconhece})
    _reconciliar(link)
    return resp


def comprovantes(chave: str):
    return list(Anexo.objects.filter(chave=chave, ativo=True).order_by("criado_em"))


def aprovar(resp: RespostaPendencia, *, usuario="", ip=None):
    resp.aprovado_em, resp.aprovado_por = timezone.now(), usuario
    resp.recusado_em, resp.recusado_por, resp.motivo_recusa = None, "", ""
    resp.save(update_fields=["aprovado_em", "aprovado_por", "recusado_em", "recusado_por", "motivo_recusa"])
    auditoria.registrar(None, "AJUSTE_MANUAL", "OK", competencia=resp.link.competencia,
                        representante=resp.link.representante, usuario=usuario, ip=ip,
                        detalhe={"resposta_pendencia": resp.pk, "aprovada": True})
    _reconciliar(resp.link)


def recusar(resp: RespostaPendencia, motivo: str, *, usuario="", ip=None):
    motivo = " ".join((motivo or "").split())[:500]
    if len(motivo) < 3:
        raise PendenciaInvalida("Escreva o motivo da recusa (o representante vai ver).")
    resp.recusado_em, resp.recusado_por, resp.motivo_recusa = timezone.now(), usuario, motivo
    resp.aprovado_em, resp.aprovado_por = None, ""
    resp.save(update_fields=["recusado_em", "recusado_por", "motivo_recusa", "aprovado_em", "aprovado_por"])
    auditoria.registrar(None, "AJUSTE_MANUAL", "OK", competencia=resp.link.competencia,
                        representante=resp.link.representante, usuario=usuario, ip=ip,
                        detalhe={"resposta_pendencia": resp.pk, "recusada": True})
    _reconciliar(resp.link)


def revogar(link: LinkPendencias, *, usuario="", ip=None):
    link.revogado_em, link.revogado_por = timezone.now(), usuario
    link.save(update_fields=["revogado_em", "revogado_por"])
    auditoria.registrar(None, "CADASTRO", "OK", competencia=link.competencia, representante=link.representante,
                        usuario=usuario, ip=ip, detalhe={"link_pendencias": link.pk, "revogado": True})


def excluir_teste(link: LinkPendencias, *, usuario="", ip=None, registrar=True) -> dict:
    """Limpeza de teste (só administrador): apaga o link, as respostas que estão nele, os comprovantes que o
    representante mandou por ele e as linhas do registro ligadas a eles. No lugar fica UMA linha dizendo o que foi
    apagado e por quem (registrar=False só na limpeza feita direto no servidor)."""
    if link.competencia.fechada:
        raise PendenciaInvalida("Competência fechada: não dá para excluir.")
    respostas = list(link.respostas.all())
    ids = [r.pk for r in respostas]
    anexos = list(Anexo.objects.filter(competencia=link.competencia, chave__in={r.chave for r in respostas},
                                       usuario=f"representante: {link.representante.nome} (link)"))
    caminhos = {a.arquivo_caminho for a in anexos}
    with transaction.atomic():
        registros = Auditoria.objects.filter(Q(detalhe__link_pendencias=link.pk) | Q(detalhe__link=link.pk)
                                             | Q(detalhe__resposta_pendencia__in=ids))
        resumo = {"links": 1, "respostas": len(ids), "comprovantes": len(anexos), "registros": registros.count()}
        registros.delete()
        Anexo.objects.filter(pk__in=[a.pk for a in anexos]).delete()
        RespostaPendencia.objects.filter(pk__in=ids).delete()
        competencia, rep = link.competencia, link.representante
        link.delete()
    for caminho in caminhos:   # o arquivo pode ser o mesmo de outro comprovante (guardado pelo hash)
        if not Anexo.objects.filter(arquivo_caminho=caminho).exists():
            Path(caminho).unlink(missing_ok=True)
    if registrar:
        auditoria.registrar(None, "EXCLUSAO_TESTE", "OK", competencia=competencia, representante=rep,
                            usuario=usuario, ip=ip, detalhe=resumo)
    link.competencia, link.representante = competencia, rep
    _reconciliar(link)
    return resumo


def _reconciliar(link: LinkPendencias):
    cartoes = list(Cartao.objects.filter(representante=link.representante,
                                         escopo=Cartao.Escopo.CONCILIAR_FORMULARIO))
    if not link.competencia.fechada:
        motor.executar(link.competencia, cartoes, motivo="pendência respondida", usuario="portal")
