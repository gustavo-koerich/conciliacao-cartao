"""Importa a planilha GERAL REPRESENTANTES para as competências escolhidas de um banco.

    python manage.py importar_geral "GERAL REPRESENTANTES.xlsx" --conta ailos --meses 2026-01 2026-02 ...
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from conciliacao.models import Competencia
from conciliacao.planilha import servico
from conciliacao.planilha.normalizacao import PlanilhaInvalida


class Command(BaseCommand):
    help = "Importa a planilha geral (todos os representantes) nas competências escolhidas."

    def add_arguments(self, parser):
        parser.add_argument("arquivo", type=Path)
        parser.add_argument("--conta", default="ailos")
        parser.add_argument("--meses", nargs="+", required=True, help="AAAA-MM das faturas (competências).")

    def handle(self, *args, arquivo: Path, conta: str, meses: list, **opcoes):
        if not arquivo.is_file():
            raise CommandError(f"Arquivo não encontrado: {arquivo}")
        comps = list(Competencia.objects.filter(conta__slug=conta, ano_mes__in=meses))
        faltam = sorted(set(meses) - {c.ano_mes for c in comps})
        if faltam:
            raise CommandError(f"Sem fatura importada para: {', '.join(faltam)} ({conta}).")
        try:
            resumo = servico.importar_geral(arquivo, arquivo.name, comps, usuario="comando:importar_geral")
        except PlanilhaInvalida as exc:
            raise CommandError(str(exc)) from exc
        if resumo["criados"]:
            self.stdout.write(self.style.WARNING(f"Representantes criados: {', '.join(resumo['criados'])}"))
        for comp, reps in resumo["competencias"].items():
            self.stdout.write(f"{comp}: " + ", ".join(f"{n} {q}" for n, q in sorted(reps.items())))
