"""python manage.py limpar_base --confirmar [--com-cadastros]

Apaga o movimento (faturas, planilhas, notas, controle, resultados, ajustes, anexos, auditoria) e os arquivos
guardados, para começar do zero (ex.: antes de entregar aos usuários). Mantém, por padrão, os cadastros:
bancos, cartões, representantes e parâmetros. Faça uma exportação/backup antes: não há como desfazer.
"""
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from conciliacao import auditoria, exportacao
from conciliacao.models import Cartao, ContaCartao, Parametro, Representante

CADASTROS = (ContaCartao, Representante, Cartao, Parametro)
PASTAS_DE_MOVIMENTO = ("faturas", "planilhas", "recibos", "comprovantes", "notas", "controle", "base")


class Command(BaseCommand):
    help = "Apaga o movimento e os arquivos guardados; mantém os cadastros (a não ser com --com-cadastros)."

    def add_arguments(self, parser):
        parser.add_argument("--confirmar", action="store_true", help="Obrigatório: confirma que é para apagar.")
        parser.add_argument("--com-cadastros", action="store_true", help="Apaga também bancos, cartões e representantes.")

    def handle(self, *args, confirmar=False, com_cadastros=False, **opts):
        if not confirmar:
            raise CommandError("Nada foi apagado. Rode de novo com --confirmar (faça backup antes).")
        manter = set() if com_cadastros else {m._meta.db_table for m in CADASTROS}
        tabelas = [m._meta.db_table for m in exportacao.modelos() if m._meta.db_table not in manter]
        # Nenhum cadastro mantido aponta para tabela de movimento; o CASCADE não alcança o que fica.
        for m in CADASTROS:
            if m._meta.db_table in manter:
                alvo = {f.related_model._meta.db_table for f in m._meta.concrete_fields if f.is_relation}
                if alvo & set(tabelas):
                    raise CommandError(f"{m._meta.db_table} aponta para tabela que seria apagada: {alvo & set(tabelas)}")
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")  # FKs do Django são adiadas; o TRUNCATE não aceita pendências
            cur.execute("TRUNCATE " + ", ".join(connection.ops.quote_name(t) for t in tabelas) + " RESTART IDENTITY CASCADE")
        raiz = Path(settings.ARQUIVOS_DIR)
        for pasta in PASTAS_DE_MOVIMENTO:
            if (raiz / pasta).is_dir():
                shutil.rmtree(raiz / pasta)
        auditoria.registrar(None, "CADASTRO", "OK", usuario="linha de comando",
                            detalhe={"base_limpa": True, "tabelas": len(tabelas), "cadastros_mantidos": not com_cadastros})
        self.stdout.write(f"Base limpa: {len(tabelas)} tabelas zeradas"
                          + ("" if com_cadastros else "; cadastros mantidos") + ".")
