"""Avisos de conferência para o financeiro. Só leitura: não mudam situação, valor nem nenhum dado gravado; quem
decide é a pessoa, olhando o recibo (pedido de 26/10/2026, relatório de auditoria itens 2, 4 e 6).

- possível cobrança em dobro: mesmo cartão, data, valor e estabelecimento repetidos na mesma fatura;
- estorno provável: crédito sem compra na mesma fatura, com uma compra de mesmo valor em fatura anterior;
- edições soltas: ajuste, comprovante, finalidade ou categoria presos a um lançamento que não existe mais na
  versão da fatura em uso (ex.: "Ler de novo" corrigiu a descrição).
"""
from collections import Counter

from .models import (AjusteManual, Anexo, CorrecaoDespesa, FaturaLanc, FinalidadeLancamento, Importacao,
                     chave_lancamento)


def cobrancas_repetidas(fatura) -> dict:
    """{lancamento_id: quantas vezes aparece} para compras repetidas na mesma fatura (cartão, data, valor, descrição)."""
    if fatura is None:
        return {}
    lancs = list(FaturaLanc.objects.filter(importacao=fatura, secao=FaturaLanc.Secao.PORTADOR, valor__gt=0)
                 .values_list("pk", "cartao_id", "data", "valor", "descricao"))
    vezes = Counter(l[1:] for l in lancs)
    return {l[0]: vezes[l[1:]] for l in lancs if vezes[l[1:]] > 1}


def estornos_provaveis(creditos) -> dict:
    """{crédito_id: compra} para créditos (valor negativo) cuja compra de mesmo valor está numa fatura anterior do
    mesmo cartão. Se houver mais de uma, a de data mais próxima."""
    creditos = [c for c in creditos if c.valor < 0 and c.cartao_id]
    if not creditos:
        return {}
    compras = (FaturaLanc.objects.filter(importacao__tipo=Importacao.Tipo.FATURA, importacao__ativo=True,
                                         cartao_id__in={c.cartao_id for c in creditos},
                                         valor__in={-c.valor for c in creditos}, natureza=FaturaLanc.Natureza.COMPRA)
               .select_related("importacao__competencia__conta"))
    saida = {}
    for c in creditos:
        candidatas = [p for p in compras if p.cartao_id == c.cartao_id and p.valor == -c.valor and p.data <= c.data
                      and p.importacao_id != c.importacao_id]
        if candidatas:
            saida[c.pk] = min(candidatas, key=lambda p: (c.data - p.data).days)
    return saida


def edicoes_soltas(competencia, fatura) -> list:
    """[(tipo, chave)] das edições desta competência presas a lançamento que não está na fatura em uso."""
    if fatura is None:
        return []
    vivas = {chave_lancamento(l) for l in FaturaLanc.objects.filter(importacao=fatura).select_related("cartao")}
    soltas = []
    for tipo, qs in (("situação ajustada à mão", AjusteManual.objects.filter(competencia=competencia, ativo=True)),
                     ("comprovante", Anexo.objects.filter(competencia=competencia, ativo=True)),
                     ("finalidade/evento", FinalidadeLancamento.objects.filter(competencia=competencia, ativo=True))):
        soltas += [(tipo, x.chave) for x in qs if x.chave.startswith("F|") and x.chave not in vivas]
    # Categoria escolhida em linha sem formulário: não guarda a competência. Fica solta se a chave não está em
    # NENHUMA fatura em uso (a compra pode ter sido cobrada no mês seguinte); mostra as desta fatura (por cartão).
    finais = set(FaturaLanc.objects.filter(importacao=fatura).values_list("cartao__final", flat=True))
    candidatas = [c for c in CorrecaoDespesa.objects.filter(ativo=True, chave__startswith="F|")
                  if c.chave not in vivas and c.chave.split("|")[1] in finais
                  and competencia.periodo_ini.isoformat() <= c.chave.split("|")[2] <= competencia.fechamento.isoformat()]
    if candidatas:
        todas = {chave_lancamento(l) for l in FaturaLanc.objects.filter(
            importacao__tipo=Importacao.Tipo.FATURA, importacao__ativo=True).select_related("cartao")}
        soltas += [("categoria", c.chave) for c in candidatas if c.chave not in todas]
    return soltas


def descrever(chave: str) -> str:
    """Chave "F|final|AAAA-MM-DD|descrição|valor|n" em texto legível."""
    partes = chave.split("|")
    if len(partes) < 5:
        return chave
    _, final, data, descricao, valor = partes[:5]
    return f"cartão {final} · {data[8:10]}/{data[5:7]}/{data[:4]} · {descricao} · R$ {valor}"
