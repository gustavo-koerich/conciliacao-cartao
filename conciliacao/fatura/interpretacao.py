"""Transforma os segmentos lidos (texto ou OCR) em seções e lançamentos, e confere os totais.

Regras:
- datas sem ano: o ano vem do fechamento; data posterior ao fechamento é do ano anterior (parcelas antigas);
- "01/02", "10/10" na descrição: parcela atual/total (o token sai da descrição);
- valor negativo: estorno/crédito; na seção da conta, saldo anterior e pagamento ficam fora dos encargos;
- compra em moeda estrangeira: a faixa da data não tem valor; a faixa seguinte traz "US$ … V.DOL …" e o valor em R$.

TRAVA: cada portador tem de somar exatamente o "TOTAL DE X"; a soma geral (portadores + conta, incluindo saldo
anterior e pagamento) tem de bater com o total da fatura. Qualquer linha com valor que não pôde ser lida quebra a
soma da seção, e a seção fica com ERRO NA LEITURA.
"""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

MESES = {"JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
         "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12}

_SUJEIRA = "\"'“”‘’~–—_"
# Valores em R$: formato brasileiro "1.234,56" ou, em algumas faturas (ex.: maio/2026), americano "1,234.56".
# Americano só quando termina em ponto + exatamente 2 dígitos — o brasileiro nunca termina assim. Cotações e valores
# em moeda estrangeira continuam no brasileiro ("V.DOL 4,9653", "U$ 297,10"); a trava de totais confere tudo.
_NUM_BR = r"\d{1,3}(?:\.\d{3})*,\d{2}"
_NUM_US = r"\d{1,3}(?:,\d{3})*\.\d{2}"
_NUM_QQ = rf"(?:{_NUM_US}|{_NUM_BR})(?!\d)"
_RE_VALOR = re.compile(rf"({_NUM_QQ})$")
_RE_PARCELA = re.compile(r"(?:^|\s)(\d{2})/(\d{2})(?=\s|$)")
# Nome do portador + final. O nome aceita sujeira de OCR (ex.: "C" lido como "€"), mas não dígitos nem "R$".
_RE_PORTADOR = re.compile(r"^([A-ZÀ-Ú][^\d$]*[A-ZÀ-Ú€]\S*)\s+(\d{4})$")
_RE_TOTAL_DE = re.compile(rf"^TOTAL\s+DE\s+(.+?)\s+(-?R\S{{0,2}}\s*-?{_NUM_QQ})$")
_RE_TOTAL_GERAL = re.compile(rf"^TOTAL\s+(-?R\S{{0,2}}\s*-?{_NUM_QQ})$")


class ErroCabecalho(Exception):
    """Resumo da fatura (período, fechamento, vencimento, total) não lido com segurança. `parcial` traz o que deu
    para ler e `trecho` o texto como foi lido, para a pessoa conferir e seguir ou mandar ao suporte."""
    parcial: dict = {}
    trecho: str = ""


@dataclass
class Lancamento:
    secao: str                      # "PORTADOR" | "CONTA"
    natureza: str
    data: date | None
    descricao: str
    cidade: str
    valor: Decimal
    pagina: int
    ordem: int = 0
    parcela_atual: int | None = None
    parcela_total: int | None = None
    moeda: str = "BRL"
    valor_moeda: Decimal | None = None
    cotacao: Decimal | None = None


@dataclass
class Secao:
    final: str | None               # None = movimentações da conta
    nome: str
    pagina_ini: int
    pagina_fim: int = 0
    total_declarado: Decimal | None = None
    lancamentos: list = field(default_factory=list)
    problemas: list = field(default_factory=list)   # linhas não reconhecidas desta seção
    diferenca: Decimal | None = None
    ok: bool = False

    @property
    def soma(self) -> Decimal:
        return sum((l.valor for l in self.lancamentos), Decimal("0"))


@dataclass
class FaturaLida:
    metodo: str
    periodo_ini: date
    periodo_fim: date
    fechamento: date
    vencimento: date
    total_cabecalho: Decimal
    total_linha: Decimal | None = None
    conta: Secao | None = None
    portadores: list = field(default_factory=list)
    problemas: list = field(default_factory=list)
    erros: list = field(default_factory=list)

    @property
    def secoes(self):
        return ([self.conta] if self.conta else []) + self.portadores

    @property
    def lancamentos(self):
        return [l for s in self.secoes for l in s.lancamentos]

    @property
    def soma_geral(self) -> Decimal:
        return sum((s.soma for s in self.secoes), Decimal("0"))

    @property
    def encargos(self) -> Decimal:
        return sum((l.valor for l in self.lancamentos if l.natureza == "ENCARGO"), Decimal("0"))

    @property
    def fecha(self) -> bool:
        return not self.erros and all(s.ok for s in self.secoes)

    def taxa_fechamento(self):
        return sum(s.ok for s in self.portadores), len(self.portadores)


# ---------- valores e datas ----------

def ler_valor(texto: str) -> Decimal | None:
    """ "R$ 1.234,56", "R$ 1,234.56", "-R$ 12,00", "RS 12,00", "“R$12,00" (OCR) → Decimal. Sem formato exato, None."""
    t = (texto or "").replace(" ", "")
    if not t:
        return None
    m = _RE_VALOR.search(t)
    if not m:
        return None
    prefixo = t[:m.start()]
    if prefixo and not re.fullmatch(r"[" + re.escape(_SUJEIRA) + r"-]?R{0,2}[$S5]{0,2}-?", prefixo, re.I):
        return None
    bruto = m.group(1)
    numero = (Decimal(bruto.replace(",", "")) if re.fullmatch(_NUM_US, bruto)
              else Decimal(bruto.replace(".", "").replace(",", ".")))
    negativo = bool(prefixo) and (prefixo[0] in "-" + _SUJEIRA or prefixo.endswith("-"))
    return -numero if negativo else numero


_DIGITO_OCR = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "L": "1", "S": "5", "B": "8", "Z": "2"})
_LETRA_OCR = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z"})


def ler_dia_mes(texto: str):
    """"15 JUL" / "15JUL" → (15, 7). Corrige trocas comuns do OCR (O/0, I/1). None se não for data."""
    t = re.sub(r"[^A-Z0-9]", "", (texto or "").upper())
    if len(t) == 6:
        t = t[1:]  # borda da faixa lida como "O"/"I" antes da data
    if len(t) != 5:
        return None
    dia, mes = t[:2].translate(_DIGITO_OCR), t[2:].translate(_LETRA_OCR)
    if not dia.isdigit() or mes not in MESES or not 1 <= int(dia) <= 31:
        return None
    return int(dia), MESES[mes]


def com_ano(dia_mes, fechamento: date) -> date | None:
    dia, mes = dia_mes
    for ano in (fechamento.year, fechamento.year - 1):
        try:
            d = date(ano, mes, dia)
        except ValueError:
            return None
        if d <= fechamento:
            return d
    return None


def limpar_descricao(texto: str) -> str:
    """Trocas típicas do OCR que não mudam o sentido: aspas curvas no lugar de "*", "€" no lugar de "C",
    barra vertical solta no fim (borda da célula)."""
    t = re.sub(r"[“”\"]", "*", texto or "").replace("€", "C")
    t = re.sub(r"\s*[|]+\)?\s*$", "", t)
    return " ".join(t.split())


def separar_parcela(descricao: str):
    """Tira o "NN/NN" da descrição. Devolve (descrição, atual, total)."""
    achados = list(_RE_PARCELA.finditer(descricao))
    if not achados:
        return descricao.strip(), None, None
    m = achados[-1]
    atual, total = int(m.group(1)), int(m.group(2))
    if not 1 <= atual <= total:
        return descricao.strip(), None, None
    limpa = " ".join((descricao[:m.start()] + " " + descricao[m.end():]).split())
    return limpa, atual, total


# ---------- cabeçalho ----------

_DIA_OCR = str.maketrans({"O": "0", "I": "1", "L": "1", "T": "1", "S": "5", "B": "8", "Z": "2"})
_NUM = rf"({_NUM_QQ})"


def _dia(texto):
    t = texto.upper().translate(_DIA_OCR)
    return int(t) if t.isdigit() and 1 <= int(t) <= 31 else None


def _mes(texto):
    m = texto.upper().translate(_LETRA_OCR)
    return MESES.get(m)


def _votar(candidatos, nome):
    """O valor que mais aparece entre as fontes do resumo. Empate entre valores diferentes é erro."""
    if not candidatos:
        raise ErroCabecalho(f"Não encontrei no resumo da fatura: {nome}.")
    contagem = {}
    for c in candidatos:
        contagem[c] = contagem.get(c, 0) + 1
    ordenados = sorted(contagem.items(), key=lambda kv: -kv[1])
    if len(ordenados) > 1 and ordenados[0][1] == ordenados[1][1]:
        raise ErroCabecalho(f"O resumo traz {nome} divergentes: {', '.join(str(v) for v, _ in ordenados)}.")
    return ordenados[0][0]


def _proximo(dia, mes, ref: date, depois=False) -> date | None:
    """Data com o ano mais próximo de ref (antes de ref, ou depois se depois=True)."""
    for ano in ((ref.year, ref.year + 1) if depois else (ref.year, ref.year - 1)):
        try:
            d = date(ano, mes, dia)
        except ValueError:
            return None
        if (d >= ref) if depois else (d <= ref):
            return d
    return None


def _ref_valido(trecho: str) -> list:
    """ "1SETA1OUT" → [(1, 9, 1, 10)]. Testa as divisões possíveis (dia com 1 ou 2 caracteres) e fica com as que
    formam datas válidas: o "O" de OUT não pode virar dígito do dia ("1O" + "UT…")."""
    achados = []
    for t1 in (1, 2):
        d1, m1 = trecho[:t1], trecho[t1:t1 + 3]
        if trecho[t1 + 3:t1 + 4] != "A" or not (_dia(d1) and _mes(m1)):
            continue
        resto = trecho[t1 + 4:]
        for t2 in (1, 2):
            d2, m2 = resto[:t2], resto[t2:t2 + 3]
            if _dia(d2) and _mes(m2):
                achados.append((_dia(d1), _mes(m1), _dia(d2), _mes(m2)))
    return achados[:1]


def _candidatos_cabecalho(texto: str):
    """Todas as leituras de total, fechamento, período (REF) e vencimento encontradas no texto do resumo."""
    t = " ".join(texto.upper().split())
    compacto = t.replace(" ", "")
    totais = [ler_valor(v) for v in
              re.findall(r"VALOR TOTAL DE R\S{0,2}\s*" + _NUM, t)
              + re.findall(r"TOTAL VENCIMENTO R\S{0,2}\s*" + _NUM, t)
              + re.findall(r"(?<!SALDO )(?<!DE )\bTOTAL R\S{0,2}\s*" + _NUM + r"\s+\S{0,4}\s*VENCIMENTO", t)]
    fechamentos = []
    for d, m, a in re.findall(r"FECHAMENTO DA FATURA\s*(\d{2})/(\d{2})/(\d{4})", t):
        try:
            fechamentos.append(date(int(a), int(m), int(d)))
        except ValueError:
            pass
    refs = []
    for m in re.finditer(r"REF", compacto):
        refs += _ref_valido(compacto[m.end():m.end() + 12])
    vencs = []
    for d, m, a in re.findall(r"VENCIMENTO:?\s*([0-9OILT]{1,2})\s*([A-Z0-9]{3})\s*(\d{4})", t):
        if _dia(d) and _mes(m):
            try:
                vencs.append(date(int(a), _mes(m), _dia(d)))
            except ValueError:
                pass
    return [v for v in totais if v], fechamentos, refs, vencs


def cabecalho_parcial(texto: str) -> dict:
    """O que deu para ler do resumo, campo a campo, para a pessoa conferir e completar quando a leitura falha.
    Leituras empatadas ficam com a primeira (a pessoa confere)."""
    totais, fechamentos, refs, vencs = _candidatos_cabecalho(texto)

    def mais_comum(lista):
        return max(lista, key=lista.count) if lista else None

    fechamento, vencimento, ref = mais_comum(fechamentos), mais_comum(vencs), mais_comum(refs)
    parcial = {"total": mais_comum(totais), "fechamento": fechamento, "vencimento": vencimento}
    base = fechamento or (vencimento - timedelta(days=10) if vencimento else None)
    if ref and base:
        fim = _proximo(ref[2], ref[3], base + timedelta(days=10))
        parcial["periodo_fim"] = fim
        parcial["periodo_ini"] = _proximo(ref[0], ref[1], fim) if fim else None
        if not fechamento:
            parcial["fechamento"] = fim
    return {k: v for k, v in parcial.items() if v is not None}


def ler_cabecalho(texto: str) -> dict:
    """Período (REF), fechamento, vencimento e total, por votação entre as fontes do resumo.

    O PDF com texto traz cada informação uma ou duas vezes; o OCR traz várias leituras (resumo, cabeçalho de cada
    página, recortes ampliados) e algumas vêm erradas. Vale a leitura que mais se repete e que faz sentido.
    """
    totais, fechamentos, refs, vencs_brutos = _candidatos_cabecalho(texto)
    total = _votar(totais, "total da fatura")
    ref = _votar(refs, "período (REF)")
    # Sem fechamento impresso, o fim do período (REF) com o ano do vencimento faz as vezes do fechamento.
    if fechamentos:
        fechamento = _votar(fechamentos, "data de fechamento")
    else:
        base = _votar(vencs_brutos, "vencimento")
        fechamento = _proximo(ref[2], ref[3], base)
    vencs = [v for v in vencs_brutos if fechamento and 0 < (v - fechamento).days <= 40]
    vencimento = _votar(vencs, "vencimento (até 40 dias depois do fechamento)")

    periodo_fim = _proximo(ref[2], ref[3], fechamento + timedelta(days=10))
    periodo_ini = _proximo(ref[0], ref[1], periodo_fim)
    if not (periodo_ini and periodo_fim and fechamento):
        raise ErroCabecalho("Datas do período inválidas no resumo da fatura.")
    if abs((fechamento - periodo_fim).days) > 7:
        raise ErroCabecalho(f"Fechamento {fechamento:%d/%m/%Y} longe do fim do período {periodo_fim:%d/%m/%Y}.")
    return {"periodo_ini": periodo_ini, "periodo_fim": periodo_fim, "fechamento": fechamento,
            "vencimento": vencimento, "total": total}


# ---------- montagem das seções ----------

def _natureza(secao: str, descricao: str, valor: Decimal) -> str:
    if secao == "CONTA":
        d = descricao.upper()
        if d.startswith("SALDO ANTERIOR"):
            return "SALDO_ANTERIOR"
        if d.startswith("PAGAMENTO"):
            return "PAGAMENTO"
        return "ENCARGO"
    return "CREDITO" if valor < 0 else "COMPRA"


def interpretar(bruta, cabecalho: dict) -> FaturaLida:
    fat = FaturaLida(bruta.metodo, cabecalho["periodo_ini"], cabecalho["periodo_fim"], cabecalho["fechamento"],
                     cabecalho["vencimento"], cabecalho["total"])
    atual = None
    iniciado = finalizado = False
    pendente = None              # compra em moeda estrangeira esperando a faixa com o valor em R$
    ordem = 0

    def problema(seg, texto):
        registro = f"pág. {seg.pagina}: {texto}"
        (atual.problemas if atual else fat.problemas).append(registro)

    def adicionar(lanc):
        nonlocal ordem
        ordem += 1
        lanc.ordem = ordem
        atual.lancamentos.append(lanc)
        atual.pagina_fim = max(atual.pagina_fim, lanc.pagina)

    inicio = None                # (página, topo) do título "MOVIMENTAÇÕES DA CONTA"
    for seg in bruta.segmentos:
        if finalizado:
            break
        # A tabela pode começar no pé da coluna esquerda (ex.: maio/2026, pág. 3); a coluna direita da mesma página
        # é lida depois e traz, no alto, os quadros de resumo: texto solto acima do título não é lançamento.
        # (Linhas da tabela — faixas — valem sempre: na coluna direita elas podem começar um pouco acima do título.)
        if inicio and seg.tipo == "livre" and seg.pagina == inicio[0] and seg.top < inicio[1]:
            continue
        if seg.tipo == "livre":
            for linha in seg.linhas:
                texto = " ".join(linha.upper().split())
                if re.search(r"MOVIMENTA\S*ES DA CONTA", texto):
                    iniciado = True
                    inicio = (seg.pagina, seg.top)
                    atual = fat.conta = Secao(None, "Movimentações da conta", seg.pagina)
                    continue
                if not iniciado or texto.startswith("DATA DESCRI") or texto.startswith("LAN"):
                    continue
                if m := _RE_TOTAL_DE.match(texto):
                    if atual is None or atual.final is None:
                        problema(seg, f"total sem seção de portador: {linha}")
                    else:
                        atual.total_declarado = ler_valor(m.group(2))
                        atual.pagina_fim = max(atual.pagina_fim, seg.pagina)
                        atual = None
                    continue
                if m := _RE_TOTAL_GERAL.match(texto):
                    fat.total_linha = ler_valor(m.group(1))
                    finalizado = True
                    break
                if m := _RE_PORTADOR.match(texto):
                    if pendente:
                        problema(seg, f"compra em moeda estrangeira sem valor em R$: {pendente.descricao}")
                        pendente = None
                    atual = Secao(m.group(2), limpar_descricao(m.group(1)), seg.pagina, seg.pagina)
                    fat.portadores.append(atual)
                    continue
                problema(seg, f"linha não reconhecida: {linha}")
            continue

        if not iniciado:
            continue
        if atual is None:
            problema(seg, f"lançamento fora de uma seção: {seg.resumo()}")
            continue
        valor = ler_valor(seg.valor) if seg.valor else None
        descricao = limpar_descricao(" ".join(seg.descricao))

        if pendente:
            if not seg.data and valor is not None:
                info = " ".join(seg.descricao + [seg.cidade]).upper().replace(" ", "")
                us = re.search(rf"U[S5]?\$?({_NUM_QQ})", info)
                cot = re.search(r"V\.?DOL(\d+[,.]\d+)", info) or re.search(r"(\d,\d{4})", info)
                pendente.valor = valor
                pendente.moeda = "USD"
                pendente.valor_moeda = ler_valor(us.group(1)) if us else None
                pendente.cotacao = Decimal(cot.group(1).replace(",", ".")) if cot else None
                pendente.natureza = _natureza(pendente.secao, pendente.descricao, valor)
                adicionar(pendente)
                pendente = None
                continue
            problema(seg, f"compra em moeda estrangeira sem valor em R$: {pendente.descricao}")
            pendente = None

        if seg.data:
            dia_mes = ler_dia_mes(seg.data)
            data = com_ano(dia_mes, fat.fechamento) if dia_mes else None
            if data is None:
                problema(seg, f"data ilegível ({seg.data!r}): {seg.resumo()}")
                continue
            desc, p_atual, p_total = separar_parcela(descricao)
            secao = "CONTA" if atual.final is None else "PORTADOR"
            if valor is None:
                if seg.valor:
                    problema(seg, f"valor ilegível ({seg.valor!r}): {seg.resumo()}")
                elif desc:
                    pendente = Lancamento(secao, "", data, desc, seg.cidade, Decimal("0"), seg.pagina,
                                          parcela_atual=p_atual, parcela_total=p_total)
                else:
                    problema(seg, f"linha sem descrição e sem valor: {seg.resumo()}")
                continue
            adicionar(Lancamento(secao, _natureza(secao, desc, valor), data, desc, seg.cidade, valor, seg.pagina,
                                 parcela_atual=p_atual, parcela_total=p_total))
            continue

        # Faixa sem data
        if valor is not None and atual.final is None and descricao.upper().startswith("SALDO ANTERIOR"):
            adicionar(Lancamento("CONTA", "SALDO_ANTERIOR", None, "SALDO ANTERIOR", "", valor, seg.pagina))
        elif valor is None and not seg.valor:
            continue  # rótulo sem valor (ex.: "Proteção Perda ou Roubo"): não é lançamento
        else:
            problema(seg, f"valor sem data: {seg.resumo()}")

    if pendente:
        fat.problemas.append(f"compra em moeda estrangeira sem valor em R$: {pendente.descricao}")
    _conferir(fat)
    return fat


def _conferir(fat: FaturaLida):
    """A trava. Marca cada seção como OK ou ERRO e registra as diferenças."""
    for s in fat.portadores:
        if s.total_declarado is None:
            s.problemas.append(f"'TOTAL DE {s.nome.split()[0]}' não encontrado")
        else:
            s.diferenca = s.soma - s.total_declarado
        # Uma linha com valor que não foi lida deixa a soma diferente do total: a diferença já reprova a seção.
        s.ok = s.diferenca == 0
    if fat.total_linha is None:
        fat.erros.append("Linha 'TOTAL' do fim dos lançamentos não encontrada.")
    elif fat.total_linha != fat.total_cabecalho:
        fat.erros.append(f"Total do fim dos lançamentos ({fat.total_linha}) difere do resumo ({fat.total_cabecalho}).")
    if fat.conta is None:
        fat.erros.append("Seção 'Movimentações da conta' não encontrada.")
    else:
        # A conta não tem total próprio: o esperado é o total da fatura menos os totais declarados dos portadores.
        declarados = sum((s.total_declarado or Decimal("0") for s in fat.portadores), Decimal("0"))
        fat.conta.total_declarado = fat.total_cabecalho - declarados
        fat.conta.diferenca = fat.conta.soma - fat.conta.total_declarado
        fat.conta.ok = fat.conta.diferenca == 0 and not fat.conta.problemas
        for l in fat.conta.lancamentos:
            if l.data is None:
                l.data = fat.periodo_ini
    finais = [s.final for s in fat.portadores]
    repetidos = sorted({f for f in finais if finais.count(f) > 1})
    if repetidos:
        fat.erros.append(f"Cartão com mais de uma seção: {', '.join(repetidos)}.")
    if fat.problemas:
        fat.erros.append(f"{len(fat.problemas)} linha(s) fora de qualquer seção.")
    if fat.soma_geral != fat.total_cabecalho:
        fat.erros.append(f"Soma geral {fat.soma_geral} difere do total da fatura {fat.total_cabecalho} "
                         f"(diferença {fat.soma_geral - fat.total_cabecalho}).")
