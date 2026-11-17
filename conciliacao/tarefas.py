"""Tarefas de fundo (RQ). O worker roda: python manage.py rqworker default"""
import django_rq

from .fatura import servico as fatura_servico
from .planilha import servico as planilha_servico

TEMPO_MAXIMO = 1800  # segundos; OCR de uma fatura de ~17 páginas leva cerca de 1 minuto


def enfileirar_fatura(importacao_id: int):
    return django_rq.get_queue("default").enqueue(fatura_servico.processar, importacao_id,
                                                  job_timeout=TEMPO_MAXIMO)


def enfileirar_planilha(importacao_id: int):
    return django_rq.get_queue("default").enqueue(planilha_servico.processar, importacao_id,
                                                  job_timeout=TEMPO_MAXIMO)


def enfileirar_geral(caminho: str, nome_original: str, competencia_ids: list, usuario="", ip=None):
    return django_rq.get_queue("default").enqueue(planilha_servico.importar_geral_por_ids, caminho, nome_original,
                                                  competencia_ids, usuario, ip, job_timeout=TEMPO_MAXIMO)
