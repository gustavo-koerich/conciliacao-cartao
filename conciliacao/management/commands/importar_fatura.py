"""Importa uma fatura pela linha de comando, sem fila (útil para teste e para medir a taxa de fechamento).

    python manage.py importar_fatura tests/fixtures/fatura-2026-08.pdf [--ocr] [--conta bb]
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from conciliacao.fatura import servico
from conciliacao.models import ContaCartao


class Command(BaseCommand):
    help = "Lê, confere e grava uma fatura em PDF."

    def add_arguments(self, parser):
        parser.add_argument("arquivo", type=Path)
        parser.add_argument("--ocr", action="store_true", help="Força o OCR mesmo com camada de texto.")
        parser.add_argument("--conta", default="", help="Banco (slug da conta de cartão, ex.: ailos, bb). Padrão: Ailos.")

    def handle(self, *args, arquivo: Path, ocr: bool, conta: str = "", **opcoes):
        if not arquivo.is_file():
            raise CommandError(f"Arquivo não encontrado: {arquivo}")
        try:
            conta_obj = ContaCartao.objects.get(slug=conta) if conta else None
            imp, criada = servico.criar_importacao(arquivo, arquivo.name, conta=conta_obj, usuario="comando:importar_fatura")
        except servico.ArquivoRecusado as exc:
            raise CommandError(str(exc)) from exc
        if not criada:
            self.stdout.write(f"Mesmo arquivo da versão ativa (importação {imp.pk}). Nada foi criado.")
            return
        imp = servico.processar(imp.pk, forcar_ocr=ocr)
        r = imp.resumo
        if "erro" in r:
            raise CommandError(f"Importação {imp.pk}: {r['erro']}")
        self.stdout.write(
            f"Importação {imp.pk} · competência {imp.competencia} v{imp.versao} · {imp.metodo_leitura} · {imp.status}"
            f"{' · ATIVA' if imp.ativo else ''}\n"
            f"Portadores que fecharam: {r['portadores_fechados']}/{r['portadores']} · "
            f"total {r['total_fatura']} · lido {r['soma_lida']} · encargos {r['encargos_conta']} · "
            f"{r['lancamentos']} lançamentos")
        for s in imp.secoes.select_related("cartao").order_by("pagina_ini"):
            marca = "OK  " if s.status == "OK" else "ERRO"
            self.stdout.write(f"  {marca} {s.cartao.final if s.cartao else 'CONTA'} {s.nome_portador[:28]:28} "
                              f"declarado {s.total_declarado} lido {s.total_lido} dif {s.diferenca}")
        for item in r.get("erros", []) + r.get("problemas", [])[:20]:
            self.stdout.write(self.style.WARNING(f"  ! {item}"))
        if r.get("cartoes_novos"):
            self.stdout.write(self.style.WARNING(f"  Cartões novos (pendentes de cadastro): {', '.join(r['cartoes_novos'])}"))
