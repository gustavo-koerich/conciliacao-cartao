"""Carrega dados fictícios para demonstração: representantes, cartões, duas faturas e a planilha geral.

Os arquivos são gerados no formato que o sistema recebe na vida real (extrato Excel do banco e planilha do
Google Forms) e passam pelos mesmos importadores e pelo mesmo motor de conciliação da produção.
Uso:  python manage.py carregar_demo
"""
import random
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import openpyxl
import pandas as pd
from django.core.management.base import BaseCommand, CommandError

from conciliacao import analise
from conciliacao.fatura import servico as fatura
from conciliacao.fatura.bb import EMPRESA_FATURA
from conciliacao.models import Cartao, ContaCartao, Importacao, Representante
from conciliacao.planilha import servico as planilha

REPRESENTANTES = [("Fábio", "FABIO", "5501"), ("Heitor", "HEITOR", "5502"), ("Rodrigo", "RODRIGO", "5503"),
                  ("Marina", "MARINA", "5504"), ("Beatriz", "BEATRIZ", "5505")]
COMPRAS = [  # descricao na fatura, despesa no formulario, faixa de valor
    ("RESTAURANTE SABOR DA ILHA", "Alimentação", (35, 180)), ("POSTO CAMINHO DO SOL", "Combustível", (120, 320)),
    ("ESTAPAR CENTRO", "Estacionamento", (12, 45)), ("HOTEL PRAIA AZUL", "Hospedagem", (280, 720)),
    ("UBER *TRIP", "Táxi", (18, 85)), ("CAFE DO HOSPITAL", "Alimentação com médico", (40, 260)),
    ("FLORICULTURA BOUQUET", "Brindes/Presentes", (60, 190)), ("PADARIA PAO QUENTE", "Alimentação", (15, 60)),
]
MESES = ["JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO", "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO",
         "NOVEMBRO", "DEZEMBRO"]


def _br(v: Decimal) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class Command(BaseCommand):
    help = "Carrega dados fictícios de demonstração (não usar com dados reais)."

    def add_arguments(self, parser):
        parser.add_argument("--forcar", action="store_true", help="Carrega mesmo que já existam importações.")

    def handle(self, *args, forcar=False, **opcoes):
        if Importacao.objects.exists() and not forcar:
            raise CommandError("A base já tem importações. Use --forcar para carregar a demonstração mesmo assim.")
        rnd = random.Random(2026)
        conta, _ = ContaCartao.objects.get_or_create(
            slug="bb", defaults={"nome": "Banco Exemplo Empresa", "banco": "BB", "leitor": "BB"})
        for nome, _origem, final in REPRESENTANTES:
            rep, _ = Representante.objects.get_or_create(nome=nome)
            Cartao.objects.get_or_create(conta=conta, final=final,
                                         defaults={"nome_fatura": nome.upper(), "representante": rep})

        hoje = date.today()
        vencimentos = [date(hoje.year, hoje.month, 17) - timedelta(days=31 * k) for k in (2, 1)]
        pasta = Path(tempfile.mkdtemp(prefix="demo-"))
        competencias, linhas_forms = [], []
        saldo_anterior = Decimal("0")
        for venc in vencimentos:
            venc = venc.replace(day=17)
            fechamento = venc - timedelta(days=12)
            inicio = fechamento - timedelta(days=29)
            compras = []
            for nome, origem, final in REPRESENTANTES:
                for _ in range(rnd.randint(9, 16)):
                    desc, despesa, (lo, hi) = rnd.choice(COMPRAS)
                    dia = inicio + timedelta(days=rnd.randint(0, 29))
                    valor = Decimal(str(round(rnd.uniform(lo, hi), 2)))
                    compras.append((final, dia, desc, valor))
                    sorte = rnd.random()
                    if sorte < 0.80:            # formulário certo
                        linhas_forms.append((origem, dia, despesa, "Cartão Crédito", float(valor)))
                    elif sorte < 0.88:          # valor digitado errado (vira divergência)
                        linhas_forms.append((origem, dia, despesa, "Cartão Crédito", float(valor) + 10))
                    # o resto fica sem formulário (pendência para o representante)
                if rnd.random() < 0.5:
                    linhas_forms.append((origem, inicio + timedelta(days=rnd.randint(0, 29)), "Táxi", "Reembolso",
                                         round(rnd.uniform(20, 60), 2)))
            total = sum((c[3] for c in compras), Decimal("0"))
            arq = pasta / f"fatura-banco-exemplo-{venc:%Y-%m}.xlsx"
            self._extrato(arq, venc, total, saldo_anterior, compras)
            saldo_anterior = total
            imp, _ = fatura.criar_importacao(arq, arq.name, conta=conta, usuario="demo", ip="127.0.0.1")
            imp = fatura.processar(imp.pk)
            if imp.status != "OK":
                raise CommandError(f"Fatura {arq.name} não importou: {imp.resumo.get('erros')}")
            competencias.append(imp.competencia)
            self.stdout.write(f"fatura {venc:%m/%Y}: {len(compras)} lançamentos, R$ {_br(total)}")

        geral = pasta / "GERAL REPRESENTANTES.xlsx"
        df = pd.DataFrame([{"Nome da Origem": n, "Mês": MESES[d.month - 1], "Data": pd.Timestamp(d), "Despesa": desp,
                            "Descrição": "Visita a cliente", "Finalidade": rnd.choice(["Visita", "Negociação", "Evento"]),
                            "Pagamento": pag, "Médico": "", "Paciente": "", "Cidade": "Florianópolis", "Valor": v,
                            "Ano": d.year} for n, d, desp, pag, v in linhas_forms])
        with pd.ExcelWriter(geral) as w:
            pd.DataFrame({"x": [1]}).to_excel(w, sheet_name="Dashboard", index=False)
            df.to_excel(w, sheet_name="ATUALIZADOS", index=False)
        resumo = planilha.importar_geral(geral, geral.name, competencias, usuario="demo", ip="127.0.0.1")
        analise.importar_base(geral, geral.name, usuario="demo", ip="127.0.0.1")
        self.stdout.write(f"planilha geral: {len(linhas_forms)} despesas; criados: {resumo.get('criados') or '-'}")
        self.stdout.write(self.style.SUCCESS("Demonstração carregada."))

    @staticmethod
    def _extrato(caminho, venc, total, saldo_anterior, compras):
        """Extrato no layout do Excel do banco: cabeçalho, seção da conta, uma seção por cartão e Total."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Nome Cliente", EMPRESA_FATURA])
        ws.append(["Data Vencimento", f"{venc:%d/%m/%Y}"])
        ws.append(["Valor Total", _br(total)])
        ws.append([])
        ws.append(["Data", "Lancamentos", "Moeda", "Valor"])
        ws.append(["", f"0001-{EMPRESA_FATURA}", "", ""])
        ws.append(["", "SALDO FATURA ANTERIOR", "R$", _br(saldo_anterior)])
        if saldo_anterior:
            pag = venc - timedelta(days=30)
            ws.append([f"{pag:%d/%m}", "PGTO DEBITO CONTA", "R$", _br(-saldo_anterior)])
        ws.append(["Subtotal", "", "", _br(Decimal("0"))])
        for final in sorted({c[0] for c in compras}):
            ws.append(["", f"0001-{EMPRESA_FATURA} - Cartão N. {final}", "", ""])
            itens = sorted((c for c in compras if c[0] == final), key=lambda c: c[1])
            for _f, dia, desc, valor in itens:
                ws.append([f"{dia:%d/%m}", desc, "R$", _br(valor)])
            ws.append(["Subtotal", "", "", _br(sum((c[3] for c in itens), Decimal("0")))])
        ws.append(["", "Total", "", _br(total)])
        wb.save(caminho)
