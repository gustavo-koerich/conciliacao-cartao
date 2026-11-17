"""python manage.py exportar_dados [--saida ARQUIVO.zip] [--completo]"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from conciliacao import auditoria, exportacao


class Command(BaseCommand):
    help = "Exporta todas as tabelas (CSV + dicionário de dados) num .zip."

    def add_arguments(self, parser):
        parser.add_argument("--saida", help="Arquivo .zip de destino (padrão: ARQUIVOS_DIR/exportacoes/).")
        parser.add_argument("--completo", action="store_true", help="Inclui o nome do paciente (sem máscara).")

    def handle(self, *args, saida=None, completo=False, **opts):
        if not saida:
            pasta = Path(settings.ARQUIVOS_DIR) / "exportacoes"
            pasta.mkdir(parents=True, exist_ok=True)
            saida = pasta / f"conciliacao-{timezone.localtime():%Y%m%d-%H%M}.zip"
        contagem = exportacao.exportar(str(saida), completo=completo)
        auditoria.registrar(None, "EXPORTACAO", "OK", usuario="linha de comando",
                            detalhe={"arquivo": Path(saida).name, "completo": completo, "linhas": sum(contagem.values())})
        self.stdout.write(f"{saida} ({len(contagem)} tabelas, {sum(contagem.values())} linhas)")
