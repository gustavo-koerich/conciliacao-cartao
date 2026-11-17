"""Normalização da planilha de respostas do Google Forms (uma por representante), com pandas.

Regras (seção 6.2 + respostas de 06/10/2026):
- nomes de coluna com strip; colunas extras ("Unnamed", "Coluna 13", perguntas a mais) são descartadas;
- linha sem carimbo é linha vazia e é ignorada;
- data com ano < ano mínimo ou depois do carimbo: DATA_SUSPEITA, ano trocado pelo do carimbo. Se mesmo assim
  ficar depois do carimbo, data corrigida fica vazia (vai para análise);
- data em texto "M/D/AAAA" (exportação em formato americano, às vezes com ano 0026): lida como mês/dia, com a marca;
- valor: só números e "R$ 1.234,56". Qualquer outra coisa ("130 po", "Valor total 808,10 = …") é VALOR_INVALIDO;
  menção a dólar no valor é MOEDA_ESTRANGEIRA (análise);
- Pagamento "Reembolso" fica fora do cartão; "Cartão Crédito <outro nome>" é PAGAMENTO_OUTRO_CARTAO (fica fora);
- Médico canonizado pelo de-para (parâmetro);
- duplicidade: mesma data + valor + despesa com carimbos diferentes. A resposta mais antiga vale; as outras ficam
  marcadas como DUPLICADO e não conciliam.

Nada aqui escreve log com conteúdo de linha (paciente, descrição): só contagens.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import pandas as pd

from ..parametros import normalizar_texto

FUSO = ZoneInfo("America/Sao_Paulo")

COLUNAS = {
    "Carimbo de data/hora": "carimbo", "Data": "data", "Despesa": "despesa", "Descrição": "descricao",
    "Finalidade": "finalidade", "Pagamento": "pagamento", "Médico": "medico", "Paciente": "paciente",
    "Cidade": "cidade", "Valor": "valor", "Foto do Recibo/NF": "link",
}
OBRIGATORIAS = ("Carimbo de data/hora", "Data", "Despesa", "Pagamento", "Valor")

_RE_VALOR_BR = re.compile(r"^(?:R\$\s*)?(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?$", re.I)
_RE_VALOR_PONTO = re.compile(r"^(\d+)\.(\d{1,2})$")
# Milhar separado por ponto (ou vírgula) e centavos por ponto: "1.180.00", "1,180.00". Só com 2 casas no fim.
_RE_VALOR_MILHAR_PONTO = re.compile(r"^(\d{1,3}([.,])\d{3}(?:\2\d{3})*)\.(\d{2})$")
_RE_DATA_US = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{1,4})$")
_RE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")
_RE_DOLAR = re.compile(r"d[oó]lar|us\$|\busd\b|u\$", re.I)


class PlanilhaInvalida(Exception):
    pass


@dataclass
class Despesa:
    linha_planilha: int
    carimbo: datetime
    data_informada: date | None
    data_corrigida: date | None
    despesa: str
    descricao: str
    finalidade: str
    pagamento: str
    medico_original: str
    medico_canonico: str
    paciente: str
    cidade: str
    valor: Decimal | None
    valor_original: str
    link_comprovante: str
    data_suspeita: bool = False
    duplicado: bool = False
    valor_invalido: bool = False
    pagamento_outro_cartao: bool = False
    moeda_estrangeira: bool = False


@dataclass
class Resultado:
    despesas: list = field(default_factory=list)
    linhas_vazias: int = 0
    colunas_descartadas: list = field(default_factory=list)

    def contagens(self):
        d = self.despesas
        return {
            "linhas": len(d),
            "linhas_vazias": self.linhas_vazias,
            "cartao": sum(1 for x in d if _eh_cartao(x.pagamento) and not x.pagamento_outro_cartao),
            "reembolso": sum(1 for x in d if _eh_reembolso(x.pagamento)),
            "data_suspeita": sum(x.data_suspeita for x in d),
            "data_sem_correcao": sum(1 for x in d if x.data_suspeita and x.data_corrigida is None),
            "duplicado": sum(x.duplicado for x in d),
            "valor_invalido": sum(x.valor_invalido for x in d),
            "pagamento_outro_cartao": sum(x.pagamento_outro_cartao for x in d),
            "moeda_estrangeira": sum(x.moeda_estrangeira for x in d),
            "sem_link_valido": sum(1 for x in d if not x.link_comprovante.lower().startswith("http")),
            "colunas_descartadas": self.colunas_descartadas,
        }


def _eh_reembolso(pagamento: str) -> bool:
    return normalizar_texto(pagamento).startswith("reembolso")


def _eh_cartao(pagamento: str) -> bool:
    return normalizar_texto(pagamento).startswith("cartao")


def _texto(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
        return ""
    return " ".join(str(v).split())


def ler_valor(bruto) -> tuple[Decimal | None, bool, bool]:
    """Devolve (valor, invalido, moeda_estrangeira). Determinístico: sem formato reconhecido, não há valor."""
    if bruto is None or (isinstance(bruto, float) and pd.isna(bruto)):
        return None, True, False
    if isinstance(bruto, bool):
        return None, True, False
    if isinstance(bruto, (int, float, Decimal)):
        try:
            valor = Decimal(str(bruto)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None, True, False
        return (valor, False, False) if valor > 0 else (None, True, False)
    texto = _texto(bruto)
    if _RE_DOLAR.search(texto):
        return None, True, True
    texto = re.sub(r"^R\$\s*", "", texto, flags=re.I).strip()
    m = _RE_VALOR_BR.match(texto)
    if m:
        inteiro = m.group(1).replace(".", "")
        valor = Decimal(f"{inteiro}.{(m.group(2) or '0').ljust(2, '0')}")
        return (valor, False, False) if valor > 0 else (None, True, False)
    m = _RE_VALOR_PONTO.match(texto)
    if m:
        return Decimal(f"{m.group(1)}.{m.group(2).ljust(2, '0')}"), False, False
    m = _RE_VALOR_MILHAR_PONTO.match(texto)
    if m:
        valor = Decimal(f"{m.group(1).replace(m.group(2), '')}.{m.group(3)}")
        return (valor, False, False) if valor > 0 else (None, True, False)
    return None, True, False


def _ler_data(bruto):
    """Devolve (data, veio_em_texto_americano)."""
    if bruto is None or bruto is pd.NaT or (isinstance(bruto, float) and pd.isna(bruto)):
        return None, False
    if isinstance(bruto, (pd.Timestamp, datetime)):
        return bruto.date(), False
    if isinstance(bruto, date):
        return bruto, False
    texto = _texto(bruto)
    if _RE_ISO.match(texto):
        try:
            return date.fromisoformat(texto[:10]), False
        except ValueError:
            return None, False
    m = _RE_DATA_US.match(texto)
    if m:
        mes, dia, ano = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(max(ano, 1), mes, dia), True
        except ValueError:
            return None, True
    return None, False


def _corrigir_data(informada: date | None, carimbo: datetime, ano_minimo: int, texto_americano: bool):
    """Devolve (data_corrigida, suspeita)."""
    if informada is None:
        return None, True
    dia_carimbo = carimbo.date()
    suspeita = texto_americano or informada.year < ano_minimo or informada > dia_carimbo
    if not suspeita:
        return informada, False
    try:
        corrigida = informada.replace(year=carimbo.year)
    except ValueError:  # 29/02 em ano não bissexto
        return None, True
    return (corrigida if corrigida <= dia_carimbo else None), True


def _carimbo(bruto):
    if bruto is None or bruto is pd.NaT or (isinstance(bruto, float) and pd.isna(bruto)):
        return None
    # Texto ISO ("2026-07-06 11:11:02") é lido como ISO; texto brasileiro ("06/07/2026 11:11:02"), dia primeiro.
    iso = isinstance(bruto, str) and bool(_RE_ISO.match(bruto.strip()))
    ts = pd.to_datetime(bruto, errors="coerce", dayfirst=not iso)
    if pd.isna(ts):
        return None
    ts = ts.to_pydatetime().replace(microsecond=(ts.microsecond // 1000) * 1000)
    return ts.replace(tzinfo=FUSO) if ts.tzinfo is None else ts


def normalizar(caminho, parametros: dict) -> Resultado:
    try:
        planilhas = pd.read_excel(caminho, sheet_name=None, dtype=object)
    except Exception as exc:  # noqa: BLE001 - arquivo corrompido ou não-xlsx
        raise PlanilhaInvalida(f"Não foi possível abrir a planilha: {exc.__class__.__name__}.") from exc
    df = None
    for _, aba in planilhas.items():
        aba.columns = [str(c).strip() for c in aba.columns]
        if all(c in aba.columns for c in OBRIGATORIAS):
            df = aba
            break
    if df is None:
        raise PlanilhaInvalida("Nenhuma aba tem as colunas do formulário (Carimbo de data/hora, Data, Despesa, "
                               "Pagamento, Valor).")

    resultado = Resultado(colunas_descartadas=[c for c in df.columns if c not in COLUNAS])
    df = df[[c for c in df.columns if c in COLUNAS]].rename(columns=COLUNAS)
    for coluna in COLUNAS.values():
        if coluna not in df.columns:
            df[coluna] = None

    de_para = parametros["de_para_medicos"]
    ano_minimo = parametros["ano_minimo_data"]
    for indice, linha in df.iterrows():
        carimbo = _carimbo(linha["carimbo"])
        if carimbo is None:
            resultado.linhas_vazias += 1
            continue
        informada, americana = _ler_data(linha["data"])
        corrigida, suspeita = _corrigir_data(informada, carimbo, ano_minimo, americana)
        valor, invalido, dolar = ler_valor(linha["valor"])
        pagamento = _texto(linha["pagamento"])
        medico = _texto(linha["medico"])
        resultado.despesas.append(Despesa(
            linha_planilha=int(indice) + 2,  # +1 do cabeçalho, +1 porque o Excel começa em 1
            carimbo=carimbo, data_informada=informada if informada and informada.year >= 1900 else None,
            data_corrigida=corrigida, despesa=_texto(linha["despesa"]), descricao=_texto(linha["descricao"]),
            finalidade=_texto(linha["finalidade"]), pagamento=pagamento, medico_original=medico,
            medico_canonico=de_para.get(normalizar_texto(medico), medico), paciente=_texto(linha["paciente"]),
            cidade=_texto(linha["cidade"]), valor=valor, valor_original=_texto(linha["valor"])[:200],
            link_comprovante=_texto(linha["link"]), data_suspeita=suspeita, valor_invalido=invalido,
            moeda_estrangeira=dolar,
            pagamento_outro_cartao=_eh_cartao(pagamento) and normalizar_texto(pagamento) != "cartao credito",
        ))

    _marcar_duplicados(resultado.despesas)
    _desempatar_carimbos(resultado.despesas)
    return resultado


COLUNAS_GERAL = ("Nome da Origem", "Data", "Despesa", "Pagamento", "Valor")


def _carimbo_sintetico(nome, data: date, d: dict, vez: int) -> datetime:
    """A planilha geral não tem carimbo do Forms. A chave da despesa (ajustes, anexos, correções) precisa ser
    estável entre envios: meio-dia da data + deslocamento tirado do conteúdo da linha (e de quantas vezes a mesma
    linha já apareceu, para linhas idênticas). O nome do paciente não entra no cálculo."""
    import hashlib
    base = "|".join(str(x) for x in (normalizar_texto(nome), data, d["despesa"], d["descricao"], d["valor_original"],
                                     d["cidade"], d["pagamento"], vez))
    deslocamento = int(hashlib.sha256(base.encode()).hexdigest()[:12], 16) % 36_000_000   # até 10 h, em ms
    return datetime(data.year, data.month, data.day, 12, tzinfo=FUSO) + pd.Timedelta(milliseconds=deslocamento).to_pytimedelta()


def normalizar_geral(caminho, parametros: dict) -> dict:
    """Planilha GERAL REPRESENTANTES (todos os representantes numa aba, com "Nome da Origem"):
    {nome da origem: Resultado}. Diferenças em relação ao Forms: sem carimbo (sintético, estável), a data é a
    informada (sem correção pelo carimbo) e linhas iguais não são duplicidade — a planilha é montada pelo financeiro
    e duas despesas iguais no mesmo dia (ex.: dois estacionamentos) são reais."""
    try:
        planilhas = pd.read_excel(caminho, sheet_name=None, dtype=object)
    except Exception as exc:  # noqa: BLE001
        raise PlanilhaInvalida(f"Não foi possível abrir a planilha: {exc.__class__.__name__}.") from exc
    df = None
    for nome_aba in sorted(planilhas, key=lambda n: n.strip().upper() != "ATUALIZADOS"):
        aba = planilhas[nome_aba]
        aba.columns = [str(c).strip() for c in aba.columns]
        if all(c in aba.columns for c in COLUNAS_GERAL):
            df = aba
            break
    if df is None:
        raise PlanilhaInvalida("Nenhuma aba tem as colunas da planilha geral (Nome da Origem, Data, Despesa, "
                               "Pagamento, Valor).")
    de_para, ano_minimo = parametros["de_para_medicos"], parametros["ano_minimo_data"]
    por_nome, vezes = {}, {}
    for indice, linha in df.iterrows():
        nome = _texto(linha.get("Nome da Origem"))
        informada, americana = _ler_data(linha.get("Data"))
        if not nome or informada is None or informada.year < 2000:
            continue
        valor, invalido, dolar = ler_valor(linha.get("Valor"))
        pagamento, medico = _texto(linha.get("Pagamento")), _texto(linha.get("Médico"))
        d = {"despesa": _texto(linha.get("Despesa")), "descricao": _texto(linha.get("Descrição")),
             "valor_original": _texto(linha.get("Valor"))[:200], "cidade": _texto(linha.get("Cidade")),
             "pagamento": pagamento}
        chave = (normalizar_texto(nome), informada, tuple(d.values()))
        vezes[chave] = vezes.get(chave, 0) + 1
        suspeita = americana or informada.year < ano_minimo
        resultado = por_nome.setdefault(nome.upper(), Resultado())
        resultado.despesas.append(Despesa(
            linha_planilha=int(indice) + 2, carimbo=_carimbo_sintetico(nome, informada, d, vezes[chave]),
            data_informada=informada, data_corrigida=None if informada.year < ano_minimo else informada,
            despesa=d["despesa"], descricao=d["descricao"], finalidade=_texto(linha.get("Finalidade")),
            pagamento=pagamento, medico_original=medico, medico_canonico=de_para.get(normalizar_texto(medico), medico),
            paciente=_texto(linha.get("Paciente")), cidade=d["cidade"], valor=valor,
            valor_original=d["valor_original"], link_comprovante="", data_suspeita=suspeita,
            valor_invalido=invalido, moeda_estrangeira=dolar,
            pagamento_outro_cartao=_eh_cartao(pagamento) and normalizar_texto(pagamento) != "cartao credito"))
    for resultado in por_nome.values():
        _desempatar_carimbos(resultado.despesas)
    return por_nome


def _marcar_duplicados(despesas):
    vistos = set()
    for d in sorted(despesas, key=lambda d: d.carimbo):
        if d.valor is None or d.data_corrigida is None:
            continue
        chave = (d.data_corrigida, d.valor, normalizar_texto(d.despesa))
        if chave in vistos:
            d.duplicado = True
        else:
            vistos.add(chave)


def _desempatar_carimbos(despesas):
    """Carimbo é a chave natural. O Forms grava milissegundos; se ainda assim repetir, soma 1 ms na segunda."""
    usados = set()
    for d in sorted(despesas, key=lambda d: (d.carimbo, d.linha_planilha)):
        while d.carimbo in usados:
            d.carimbo = d.carimbo + pd.Timedelta(milliseconds=1).to_pytimedelta()
        usados.add(d.carimbo)
