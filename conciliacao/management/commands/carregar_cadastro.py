"""Carga inicial de representantes e cartões a partir de um CSV fora do git.

Formato (separador ";", com cabeçalho):
    final;nome_fatura;representante;email;sheet_id;escopo
    5501;FABIO LUIZ N SALES;Fábio;;;CONCILIAR_FORMULARIO

Pode rodar de novo: atualiza o que mudou e não apaga nada. E-mail e sheet_id vazios não sobrescrevem valores
já cadastrados. Representante sem cartão: deixe "final" vazio.
"""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from conciliacao import auditoria
from conciliacao.models import Cartao, Representante, conta_padrao

COLUNAS = ["final", "nome_fatura", "representante", "email", "sheet_id", "escopo"]


class Command(BaseCommand):
    help = "Carrega representantes e cartões de um CSV (final;nome_fatura;representante;email;sheet_id;escopo)."

    def add_arguments(self, parser):
        parser.add_argument("arquivo", type=Path)

    def handle(self, *args, arquivo: Path, **opcoes):
        if not arquivo.is_file():
            raise CommandError(f"Arquivo não encontrado: {arquivo}")
        with arquivo.open(encoding="utf-8-sig", newline="") as f:
            leitor = csv.DictReader(f, delimiter=";")
            if [c.strip() for c in (leitor.fieldnames or [])] != COLUNAS:
                raise CommandError(f"Cabeçalho esperado: {';'.join(COLUNAS)}")
            linhas = [{k.strip(): (v or "").strip() for k, v in linha.items()} for linha in leitor]

        escopos = set(Cartao.Escopo.values)
        contagem = {"representantes_novos": 0, "cartoes_novos": 0, "cartoes_alterados": 0}
        with transaction.atomic():
            for n, linha in enumerate(linhas, start=2):
                if not linha["representante"]:
                    raise CommandError(f"Linha {n}: representante vazio.")
                rep, criado = Representante.objects.get_or_create(nome=linha["representante"])
                contagem["representantes_novos"] += criado
                mudou = False
                for campo in ("email", "sheet_id"):
                    if linha[campo] and getattr(rep, campo) != linha[campo]:
                        setattr(rep, campo, linha[campo])
                        mudou = True
                if mudou:
                    rep.full_clean()
                    rep.save()

                if not linha["final"]:
                    continue
                escopo = linha["escopo"] or Cartao.Escopo.CONCILIAR_FORMULARIO
                if escopo not in escopos:
                    raise CommandError(f"Linha {n}: escopo inválido {escopo!r}. Use: {', '.join(sorted(escopos))}.")
                cartao = Cartao.objects.filter(conta_id=conta_padrao(), final=linha["final"]).first()
                novo = cartao is None
                cartao = cartao or Cartao(final=linha["final"])
                antes = (cartao.nome_fatura, cartao.representante_id, cartao.escopo, cartao.pendente_cadastro)
                cartao.nome_fatura = linha["nome_fatura"] or cartao.nome_fatura
                cartao.representante = rep
                cartao.escopo = escopo
                cartao.pendente_cadastro = False
                cartao.full_clean()
                if novo or antes != (cartao.nome_fatura, cartao.representante_id, cartao.escopo, False):
                    cartao.save()
                    contagem["cartoes_novos" if novo else "cartoes_alterados"] += 1

            auditoria.registrar(None, "CADASTRO", "OK", usuario="comando:carregar_cadastro",
                                detalhe={"arquivo": arquivo.name, "linhas": len(linhas), **contagem})
        self.stdout.write(self.style.SUCCESS(f"{len(linhas)} linhas lidas: {contagem}"))
