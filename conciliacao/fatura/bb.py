"""Fatura Banco do Brasil (Ourocard Empresa): PDF com texto ou Excel exportado ("Extrato").

Estrutura dos dois formatos:
- seção da conta: SALDO FATURA ANTERIOR, Pagamentos/Créditos, Outros lançamentos (IOF) → "Subtotal";
- uma seção por cartão ("<Nome> (Cartão 5506)" no PDF, "…Cartão N. 5505" no Excel) → "Subtotal";
- "Total" geral = total da fatura.
Os subtítulos (Serviços, Viagens, Compras parceladas…) não são lançamentos. Compra no exterior traz o valor em R$
na própria linha; as linhas seguintes ("*** 299,00 EURO", "equivalente a US$ …", "Cotação do Dólar …") só
informam moeda e cotação.

TRAVA (a mesma da Ailos): cada seção soma exatamente o seu Subtotal e a soma geral bate com o total da fatura.
"""
import os
import re
from datetime import date, timedelta
from decimal import Decimal

import openpyxl
import pdfplumber

from .interpretacao import ErroCabecalho, FaturaLida, Lancamento, Secao, com_ano, ler_valor
from .leitura import ErroLeitura

# Nome da empresa como aparece na fatura do banco (cabecalho de cada conta); muda pelo ambiente.
EMPRESA_FATURA = os.environ.get("EMPRESA_FATURA", "EMPRESA DEMO LTDA")

_RE_LINHA = re.compile(r"^(\d{2})/(\d{2})\s+(.+?)\s+(-?\d{1,3}(?:\.\d{3})*,\d{2})$")
_RE_PAIS = re.compile(r"\s+[A-Z]{2}$")
_RE_PARCELA = re.compile(r"\bPARC\s+(\d{2})/(\d{2})\b")
_RE_DATA = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_RE_CARTAO_PDF = re.compile(r"^(.+?)\s+\(Cart[ãa]o\s+(\d{4})\)$")
_RE_CARTAO_XLSX = re.compile(r"Cart[ãa]o\s+N\.?\s*(\d{4})")
_RE_EURO = re.compile(r"\*{3}\s*([\d.,]+)\s+([A-Z]+)")
_RE_USD = re.compile(r"equivalente a US\$\s*([\d.,]+)", re.I)
_RE_COTACAO = re.compile(r"Cota[çc][ãa]o do D[óo]lar de \d{2}/\d{2}:\s*R\$\s*([\d.,]+)", re.I)
_IGNORAR = re.compile(r"^(Data Descri|nacionais|P[áa]gina \d|Confira aqui|Detalhes da fatura|descriao|Fale conosco)", re.I)


def _data(valor_texto):
    m = _RE_DATA.search(valor_texto or "")
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def _decimal_br(texto):
    return Decimal(texto.replace(".", "").replace(",", "."))


class _Montador:
    """Recebe as linhas na ordem da fatura e monta seções e lançamentos."""

    def __init__(self, metodo, fechamento, vencimento, total, periodo_ini):
        self.fat = FaturaLida(metodo, periodo_ini, fechamento, fechamento, vencimento, total)
        self.atual = None
        self.ordem = 0
        self.ultimo = None
        self.pagina = 1

    def abrir_conta(self):
        if self.fat.conta is None:
            self.fat.conta = Secao(None, "Movimentações da conta", self.pagina)
        self.atual = self.fat.conta

    def abrir_cartao(self, final, nome):
        existente = next((s for s in self.fat.portadores if s.final == final), None)
        self.atual = existente or Secao(final, " ".join(nome.split()), self.pagina, self.pagina)
        if existente is None:
            self.fat.portadores.append(self.atual)

    def subtotal(self, valor):
        if self.atual is None:
            self.fat.problemas.append(f"pág. {self.pagina}: subtotal sem seção ({valor})")
            return
        self.atual.total_declarado = valor
        self.atual.pagina_fim = max(self.atual.pagina_fim, self.pagina)
        self.atual = None

    def lancamento(self, dia, mes, descricao, valor, sem_data=False):
        if self.atual is None:
            self.fat.problemas.append(f"pág. {self.pagina}: lançamento fora de seção: {descricao} {valor}")
            return
        descricao = " ".join(_RE_PAIS.sub("", " ".join(descricao.split())).split())
        p = _RE_PARCELA.search(descricao)
        atual, total = (int(p.group(1)), int(p.group(2))) if p else (None, None)
        if p and not 1 <= atual <= total:
            atual = total = None
        if p:
            descricao = " ".join(_RE_PARCELA.sub("", descricao).split())
        conta = self.atual.final is None
        d = self.fat.periodo_ini if sem_data else com_ano((dia, mes), self.fat.fechamento)
        if d is None:
            self.fat.problemas.append(f"pág. {self.pagina}: data inválida {dia:02d}/{mes:02d}: {descricao}")
            return
        texto = descricao.upper()
        if conta:
            natureza = ("SALDO_ANTERIOR" if texto.startswith("SALDO FATURA ANTERIOR")
                        else "PAGAMENTO" if texto.startswith("PGTO") else "ENCARGO")
        else:
            natureza = "CREDITO" if valor < 0 else "COMPRA"
        self.ordem += 1
        lanc = Lancamento("CONTA" if conta else "PORTADOR", natureza, d, descricao, "", valor, self.pagina,
                          ordem=self.ordem, parcela_atual=atual, parcela_total=total)
        self.atual.lancamentos.append(lanc)
        self.atual.pagina_fim = max(self.atual.pagina_fim, self.pagina)
        self.ultimo = lanc

    def moeda(self, texto):
        """Linhas de compra no exterior que complementam o lançamento anterior."""
        if self.ultimo is None:
            return False
        if m := _RE_EURO.search(texto):
            self.ultimo.moeda = m.group(2)[:3]
            return True
        if m := _RE_USD.search(texto):
            self.ultimo.valor_moeda = _decimal_br(m.group(1))
            if self.ultimo.moeda == "BRL":
                self.ultimo.moeda = "USD"
            return True
        if m := _RE_COTACAO.search(texto):
            # O Excel do BB corta a cotação ("R$ 5"); sem casas decimais, melhor deixar vazio do que gravar errado.
            if "," in m.group(1):
                self.ultimo.cotacao = _decimal_br(m.group(1).rstrip(".,"))
            return True
        return False


def _conferir(fat):
    """Mesma trava da Ailos; a conta aqui tem o próprio Subtotal impresso."""
    for s in fat.secoes:
        if s.total_declarado is None:
            s.problemas.append("Subtotal da seção não encontrado")
            s.diferenca = None
            s.ok = False
            continue
        s.diferenca = s.soma - s.total_declarado
        s.ok = s.diferenca == 0
    if fat.conta is None:
        fat.erros.append("Seção da conta (saldo anterior e pagamentos) não encontrada.")
    if fat.total_linha is None:
        fat.erros.append("Linha 'Total' do fim da fatura não encontrada.")
    elif fat.total_linha != fat.total_cabecalho:
        fat.erros.append(f"Total do fim ({fat.total_linha}) difere do total da fatura ({fat.total_cabecalho}).")
    if fat.problemas:
        fat.erros.append(f"{len(fat.problemas)} linha(s) não reconhecida(s).")
    if fat.soma_geral != fat.total_cabecalho:
        fat.erros.append(f"Soma geral {fat.soma_geral} difere do total da fatura {fat.total_cabecalho} "
                         f"(diferença {fat.soma_geral - fat.total_cabecalho}).")


def _inicio_periodo(fechamento, fechamento_anterior):
    return (fechamento_anterior + timedelta(days=1)) if fechamento_anterior else fechamento - timedelta(days=30)


# ---------- PDF ----------

def ler_pdf(caminho, fechamento_anterior=None, informado=None) -> FaturaLida:
    """informado: {fechamento, vencimento, total} conferidos pela pessoa quando o resumo não foi lido."""
    with pdfplumber.open(caminho) as pdf:
        paginas = [pg.extract_text() or "" for pg in pdf.pages]
    texto = "\n".join(paginas)
    if "Ourocard" not in texto and "bb.com.br" not in texto:
        raise ErroLeitura("O PDF não parece uma fatura do Banco do Brasil.")
    m_fech = re.search(r"Fatura fechada em:\s*(\d{2}/\d{2}/\d{4})", texto)
    m_venc = re.search(r"Vencimento:\s*(\d{2}/\d{2}/\d{4})", texto)
    m_total = re.search(r"Total da fatura:\s*R\$\s*([\d.]+,\d{2})", texto) or \
        re.search(r"Valor da fatura:\s*R\$\s*([\d.]+,\d{2})", texto)
    if informado:
        fechamento, vencimento, total = informado["fechamento"], informado["vencimento"], informado["total"]
    elif not (m_fech and m_venc and m_total):
        exc = ErroCabecalho("Não encontrei fechamento, vencimento e total da fatura no PDF do BB.")
        exc.parcial = {k: v for k, v in (("fechamento", m_fech and _data(m_fech.group(1))),
                                         ("vencimento", m_venc and _data(m_venc.group(1))),
                                         ("total", m_total and ler_valor(m_total.group(1)))) if v}
        exc.trecho = texto[:2000]
        raise exc
    else:
        fechamento, vencimento, total = _data(m_fech.group(1)), _data(m_venc.group(1)), ler_valor(m_total.group(1))
    m = _Montador("TEXTO", fechamento, vencimento, total, _inicio_periodo(fechamento, fechamento_anterior))
    dentro = False
    for numero, pagina in enumerate(paginas, start=1):
        m.pagina = numero
        for linha in pagina.splitlines():
            linha = " ".join(linha.split())
            if not dentro:
                dentro = linha.startswith("Detalhes da fatura")
                continue
            if not linha or _IGNORAR.match(linha) or "Valor da fatura:" in linha:
                continue
            if linha.lower().startswith(EMPRESA_FATURA.lower()):
                m.abrir_conta()
                continue
            if c := _RE_CARTAO_PDF.match(linha):
                m.abrir_cartao(c.group(2), c.group(1))
                continue
            if t := re.match(r"^Subtotal\s+(-?[\d.]+,\d{2})$", linha):
                m.subtotal(ler_valor(t.group(1)))
                continue
            if t := re.match(r"^Total\s+(-?[\d.]+,\d{2})$", linha):
                m.fat.total_linha = ler_valor(t.group(1))
                continue
            if linha.startswith("Total da fatura"):
                break
            if s := re.match(r"^SALDO FATURA ANTERIOR\s+(?:[A-Z]{2}\s+)?(-?[\d.]+,\d{2})$", linha):
                m.abrir_conta()
                m.lancamento(0, 0, "SALDO FATURA ANTERIOR", ler_valor(s.group(1)), sem_data=True)
                continue
            if l := _RE_LINHA.match(linha):
                m.lancamento(int(l.group(1)), int(l.group(2)), l.group(3), ler_valor(l.group(4)))
                continue
            if m.moeda(linha):
                continue
            # subtítulos (Pagamentos/Créditos, Serviços, Viagens…) e o resto não são lançamentos
    _conferir(m.fat)
    return m.fat


# ---------- Excel ("Extrato" exportado do BB) ----------

def _celulas(linha):
    return [("" if v is None else " ".join(str(v).split())) for v in linha]


def ler_xlsx(caminho, fechamento_anterior=None) -> FaturaLida:
    try:
        livro = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ErroLeitura(f"Não foi possível abrir a planilha do BB ({exc.__class__.__name__}).") from exc
    linhas = [_celulas(r) for r in livro.worksheets[0].iter_rows(values_only=True)]
    cabecalho = {r[0]: r[1] for r in linhas[:12] if len(r) > 1 and r[0]}
    if "Data Vencimento" not in cabecalho or "Valor Total" not in cabecalho:
        raise ErroLeitura("A planilha não tem o cabeçalho do extrato do BB (Data Vencimento, Valor Total).")
    vencimento = _data(cabecalho["Data Vencimento"])
    total = ler_valor(cabecalho["Valor Total"])
    # O Excel não traz a data de fechamento: usa a última data de lançamento (estimativa, informada no resumo).
    datas = [com_ano((int(r[0][:2]), int(r[0][3:5])), vencimento) for r in linhas
             if r and re.fullmatch(r"\d{2}/\d{2}", r[0])]
    datas = [d for d in datas if d]
    if not datas:
        raise ErroLeitura("Nenhum lançamento com data na planilha do BB.")
    fechamento = max(datas)
    m = _Montador("CSV_OFX", fechamento, vencimento, total, _inicio_periodo(fechamento, fechamento_anterior))
    m.fat.fechamento_estimado = True
    comecou = False
    for r in linhas:
        r = (r + ["", "", "", ""])[:4]
        data, texto, _moeda, valor = r
        if not comecou:
            comecou = data == "Data" and texto.startswith("Lancamentos")
            continue
        if not (data or texto or valor):
            continue
        if re.match(r"^\d+-+" + re.escape(EMPRESA_FATURA.split()[0]), texto, re.I):
            c = _RE_CARTAO_XLSX.search(texto)
            if c:
                m.abrir_cartao(c.group(1), cabecalho.get("Nome Cliente", "Portador"))
            else:
                m.abrir_conta()
            continue
        if data.lower() == "subtotal" or texto.lower() == "subtotal":
            m.subtotal(ler_valor(valor or texto if data.lower() == "subtotal" else valor))
            continue
        if data.lower() == "total" or texto.lower() == "total":
            m.fat.total_linha = ler_valor(valor if texto.lower() == "total" else texto)
            continue
        if data.upper() == "US$" or texto.upper() == "US$":
            continue
        if texto.upper().startswith("SALDO FATURA ANTERIOR") and valor:
            m.lancamento(0, 0, "SALDO FATURA ANTERIOR", ler_valor(valor), sem_data=True)
            continue
        if re.fullmatch(r"\d{2}/\d{2}", data) and valor:
            v = ler_valor(valor)
            if v is None:
                m.fat.problemas.append(f"valor ilegível: {data} {texto} {valor}")
                continue
            m.lancamento(int(data[:2]), int(data[3:5]), texto, v)
            continue
        if m.moeda(texto or data):
            continue
    _conferir(m.fat)
    return m.fat
