"""Planilha CONTROLE DE COMPRAS CARTÃO: ponte entre o lançamento da fatura e a nota fiscal.

Cada linha registra uma compra (produto, número da NF, total, valor que cai na fatura, parcelas, setor, OC).
O cartão escrito na planilha (coluna "PAGAMENTO") é ignorado: vale o cartão da fatura (decisão de 16/10/2026).

Correspondência com o lançamento (cartões de notas fiscais) — o valor manda (pedido de 16/10/2026):
- confiável: valor na fatura igual ao centavo, data a até JANELA_DIAS e mesmo número de parcelas (à vista = 1);
- só pelo valor (conciliado com ressalva): mesmo valor a até JANELA_VALOR_DIAS, com data ou parcelas diferentes;
- com mais de uma linha possível, a de data mais próxima;
- compra cancelada (valor negativo na planilha) corresponde ao crédito do mesmo valor.
"""
import datetime as dt
import logging
import re
from decimal import Decimal, InvalidOperation

import openpyxl
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from . import arquivos, auditoria
from .models import ControleCompra, Importacao
from .parametros import normalizar_texto

log = logging.getLogger(__name__)

JANELA_DIAS = 3
JANELA_VALOR_DIAS = 45
JANELA_CANCELADA_DIAS = 90
COLUNAS = {"nf": "nf", "data da compra": "data", "produto": "produto", "quantidade": "quantidade",
           "total da compra": "total", "total na fatura": "valor_fatura", "parcela": "parcelas", "setor": "setor",
           "oc": "oc", "observacao": "observacao"}


class ControleInvalido(Exception):
    pass


def _decimal(v):
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", ".")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _inteiro(v, padrao=1):
    try:
        n = int(Decimal(str(v)))
    except (InvalidOperation, ValueError, TypeError):
        return padrao
    return n if n >= 1 else padrao


def importar(origem, nome_original, *, usuario="", ip=None, maquina="") -> tuple[Importacao, bool]:
    hash_arquivo, caminho = arquivos.guardar(origem, nome_original, subpasta="controle", extensoes={".xlsx"},
                                             assinatura=b"PK\x03\x04", descricao="uma planilha .xlsx")
    ativa = Importacao.objects.filter(tipo=Importacao.Tipo.CONTROLE, ativo=True, hash_sha256=hash_arquivo).first()
    if ativa:
        return ativa, False
    try:
        livro = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ControleInvalido(f"Não foi possível abrir a planilha ({exc.__class__.__name__}).") from exc
    aba, mapa = None, None
    for candidata in livro.worksheets:
        cab = next(candidata.iter_rows(max_row=1, values_only=True), ())
        m = {COLUNAS[normalizar_texto(c)]: i for i, c in enumerate(cab) if normalizar_texto(c) in COLUNAS}
        if {"data", "valor_fatura"} <= set(m):
            aba, mapa = candidata, m
            break
    if aba is None:
        raise ControleInvalido("Nenhuma aba tem as colunas 'Data da compra' e 'TOTAL NA FATURA'.")

    def campo(linha, nome):
        i = mapa.get(nome)
        return linha[i] if i is not None and i < len(linha) else None

    linhas, ignoradas = [], 0
    with transaction.atomic():
        ultima = Importacao.objects.filter(tipo=Importacao.Tipo.CONTROLE).aggregate(v=Max("versao"))["v"] or 0
        imp = Importacao.objects.create(tipo=Importacao.Tipo.CONTROLE, versao=ultima + 1, origem=Importacao.Origem.UPLOAD,
                                        status=Importacao.Status.PROCESSANDO, arquivo_caminho=str(caminho),
                                        arquivo_nome_original=nome_original[:255], hash_sha256=hash_arquivo,
                                        usuario=usuario, ip=ip, maquina=maquina)
        for n, linha in enumerate(aba.iter_rows(min_row=2, values_only=True), start=2):
            data, valor = campo(linha, "data"), _decimal(campo(linha, "valor_fatura"))
            if isinstance(data, dt.datetime):
                data = data.date()
            if not isinstance(data, dt.date) or valor is None:
                ignoradas += 1
                continue
            nf_texto = " ".join(str(campo(linha, "nf") or "").split())
            oc = campo(linha, "oc")
            linhas.append(ControleCompra(
                importacao=imp, linha=n, nf_texto=nf_texto[:60], nf_numero=re.sub(r"\D", "", nf_texto)[:20],
                cancelada="CANCEL" in nf_texto.upper() or valor < 0, data=data,
                produto=" ".join(str(campo(linha, "produto") or "").split())[:300],
                quantidade=_decimal(campo(linha, "quantidade")), total_compra=_decimal(campo(linha, "total")),
                valor_fatura=valor, parcelas=_inteiro(campo(linha, "parcelas")),
                setor=str(campo(linha, "setor") or "")[:80], oc=oc is True or str(oc).strip().lower() in ("true", "sim", "s", "x"),
                observacao=str(campo(linha, "observacao") or "")[:300] if campo(linha, "observacao") not in (None, False) else ""))
        if not linhas:
            raise ControleInvalido("A planilha não tem compras com data e valor na fatura.")
        ControleCompra.objects.bulk_create(linhas, batch_size=1000)
        Importacao.objects.filter(tipo=Importacao.Tipo.CONTROLE, ativo=True).update(ativo=False)
        imp.status, imp.ativo, imp.ativado_em = Importacao.Status.OK, True, timezone.now()
        imp.resumo = {"linhas": len(linhas), "ignoradas": ignoradas,
                      "com_nf": sum(1 for l in linhas if l.nf_numero and not l.cancelada),
                      "canceladas": sum(1 for l in linhas if l.cancelada)}
        imp.save()
    auditoria.registrar(None, "IMPORTACAO", "OK", importacao=imp, usuario=usuario, ip=ip, detalhe=imp.resumo)
    return imp, True


def linhas_ativas():
    return list(ControleCompra.objects.filter(importacao__tipo=Importacao.Tipo.CONTROLE, importacao__ativo=True))


def encontrar(lanc, controle, usadas=frozenset()):
    """(linha do controle, ressalva) que corresponde ao lançamento, ou (None, "").

    Confiável (ressalva vazia): valor na fatura igual, data a até JANELA_DIAS e mesmo número de parcelas.
    Só pelo valor (com ressalva): valor igual (na fatura ou o total da compra = parcela × N) a até
    JANELA_VALOR_DIAS, quando as parcelas ou a data não batem."""
    n = lanc.parcela_total or 1

    def dias(c):
        return abs((c.data - lanc.data).days)

    livres = [c for c in controle if c.pk not in usadas]
    if lanc.valor < 0:
        candidatas = [c for c in livres if c.cancelada and c.valor_fatura == lanc.valor and dias(c) <= JANELA_CANCELADA_DIAS]
        return (min(candidatas, key=lambda c: (dias(c), c.linha)), "") if candidatas else (None, "")
    livres = [c for c in livres if not c.cancelada]
    fortes = [c for c in livres if c.valor_fatura == lanc.valor and c.parcelas == n and dias(c) <= JANELA_DIAS]
    if fortes:
        return min(fortes, key=lambda c: (dias(c), c.linha)), ""
    def total_bate(c):  # compra parcelada: total da compra ≈ parcela × N (arredondamento das parcelas)
        return n > 1 and c.total_compra is not None and abs(c.total_compra - lanc.valor * n) <= Decimal("0.05") * n

    fracas = [c for c in livres if dias(c) <= JANELA_VALOR_DIAS and (c.valor_fatura == lanc.valor or total_bate(c))]
    if not fracas:
        return None, ""
    c = min(fracas, key=lambda c: (dias(c), c.linha))
    detalhe = [f"compra em {c.data:%d/%m/%Y}"] if dias(c) > JANELA_DIAS else []
    if c.valor_fatura != lanc.valor:
        detalhe.append(f"total da compra {c.total_compra:.2f} = parcela × {n}".replace(".", ","))
    if c.parcelas != n:
        detalhe.append(f"{c.parcelas} parcela(s) no controle")
    return c, "achado no controle só pelo valor (" + ", ".join(detalhe or ["conferir"]) + ")"


def descricao(c) -> str:
    partes = [c.produto or "produto não informado"]
    if c.setor:
        partes.append(f"setor {c.setor}")
    if c.oc:
        partes.append("com OC")
    return "; ".join(partes)
