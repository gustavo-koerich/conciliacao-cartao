"""Exportação da base inteira: um CSV por tabela (abre no Excel), dicionário de dados e LEIA-ME, num .zip.

- Tabelas: todos os modelos do app (conc_*), na ordem de dependência (cadastros → importações → resultados).
- CSV com ";" e BOM UTF-8, valores com vírgula decimal não são convertidos (ficam como no banco: 1234.56).
- Nome de paciente sai mascarado, a não ser com completo=True (regra: nome de paciente não circula à toa).
- O dicionário (dicionario.csv) descreve cada coluna: tipo, obrigatória, chave estrangeira e o texto de ajuda do modelo.
"""
import csv
import io
import zipfile
from datetime import datetime

from django.apps import apps
from django.conf import settings
from django.utils import timezone

CAMPOS_SENSIVEIS = {"paciente"}


def modelos():
    return list(apps.get_app_config("conciliacao").get_models())


def _linha_dicionario(modelo, campo):
    destino = campo.related_model._meta.db_table if campo.is_relation and campo.related_model else ""
    return [modelo._meta.db_table, campo.column, campo.get_internal_type(), "sim" if not campo.null else "não",
            destino, str(campo.verbose_name), str(campo.help_text or "")]


def _csv(cabecalho, linhas) -> bytes:
    buf = io.StringIO()
    escritor = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    escritor.writerow(cabecalho)
    escritor.writerows(linhas)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _valor(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return timezone.localtime(v).isoformat(timespec="seconds") if timezone.is_aware(v) else v.isoformat()
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v, ensure_ascii=False, default=str)
    return v


def exportar(destino, *, completo=False) -> dict:
    """Grava o .zip em `destino` (caminho ou arquivo aberto). Devolve {tabela: linhas}."""
    contagem, dicionario = {}, []
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as zf:
        for modelo in modelos():
            campos = [c for c in modelo._meta.concrete_fields]
            dicionario += [_linha_dicionario(modelo, c) for c in campos]
            colunas = [c.attname for c in campos]
            linhas = []
            for registro in modelo.objects.order_by("pk").values_list(*colunas).iterator(chunk_size=2000):
                linha = [_valor(v) for v in registro]
                if not completo:
                    linha = [("***" if v else "") if c.name in CAMPOS_SENSIVEIS else v for c, v in zip(campos, linha)]
                linhas.append(linha)
            zf.writestr(f"tabelas/{modelo._meta.db_table}.csv", _csv([c.column for c in campos], linhas))
            contagem[modelo._meta.db_table] = len(linhas)
        zf.writestr("dicionario.csv", _csv(["tabela", "coluna", "tipo", "obrigatória", "referencia_tabela",
                                            "nome", "descricao"], dicionario))
        zf.writestr("LEIA-ME.txt", "\r\n".join([
            "Exportação da base da Conciliação de cartão (Empresa Demo)",
            f"Gerada em {timezone.localtime():%d/%m/%Y %H:%M} · versão do sistema {getattr(settings, 'APP_VERSAO', '?')}",
            f"Nome de paciente: {'completo' if completo else 'mascarado (***)'}",
            "",
            "tabelas/*.csv  — uma tabela por arquivo, separador ';', UTF-8 (abre direto no Excel).",
            "dicionario.csv — o que é cada coluna; 'referencia_tabela' indica a chave estrangeira.",
            "Ordem para recarregar em outro banco: conc_conta_cartao, conc_representante, conc_cartao, conc_competencia,",
            "conc_importacao e, depois, as demais.",
            "",
            "Contagem de linhas:",
            *[f"  {t}: {n}" for t, n in contagem.items()],
        ]))
    return contagem
