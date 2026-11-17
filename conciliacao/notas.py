"""Notas fiscais dos cartões de compras da empresa (escopo FASE2_NF): leitura, lote e casamento com a fatura.

Leitura:
- XML de NF-e (prioridade): chave de acesso, número, série, emissão, emitente (CNPJ/nome) e vNF. Lido com
  defusedxml (arquivo vem de fora; nada de entidades externas);
- PDF (DANFE), alternativa: chave de 44 dígitos, "VALOR TOTAL DA NOTA", data de emissão e CNPJ do emitente
  pelo texto do PDF. O que não for reconhecido fica como "não lida" e pode ser associado à mão.

Casamento (determinístico):
- compra à vista: valor da nota = valor do lançamento, emissão até JANELA_NOTA_DIAS da compra;
- compra parcelada k/N: valor da nota ≈ parcela × N (tolerância de R$ 0,05 por parcela), mesma janela sobre a
  data da compra (que a fatura repete em todas as parcelas). A mesma nota vale para as N parcelas, mês a mês;
- desempate: menor distância de datas, depois semelhança entre o emitente e a descrição da fatura.
O vínculo vira um Anexo do lançamento; o motor conta como "nota fiscal anexada".
"""
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber
from defusedxml import ElementTree
from django.db import transaction
from django.db.models import Max

from . import arquivos, auditoria, controle, motor
from .models import (Anexo, Cartao, Competencia, FaturaLanc, Importacao, NotaFiscal, Resultado, chave_lancamento)
from .parametros import normalizar_texto

log = logging.getLogger(__name__)

JANELA_NOTA_DIAS = 10
JANELA_VALOR_DIAS = 45   # só pelo valor (com ressalva): emissão a até 45 dias da compra
BOM_E_ESPACOS = b"\xef\xbb\xbf \t\r\n"
TOLERANCIA_PARCELA = Decimal("0.05")
NS = "{http://www.portalfiscal.inf.br/nfe}"
EXTENSOES = {".xml": b"<", ".pdf": b"%PDF-", ".zip": b"PK\x03\x04"}
MAX_ARQUIVOS_ZIP = 500
MAX_BYTES_ENTRADA_ZIP = 10 * 1024 * 1024
PALAVRAS_GENERICAS = {"ltda", "me", "epp", "sa", "s/a", "eireli", "comercio", "comercial", "industria", "servicos",
                      "de", "da", "do", "e", "brasil", "br", "parc", "mp", "ec"}


@dataclass
class DadosNota:
    formato: str
    chave: str = ""
    numero: str = ""
    serie: str = ""
    emissao: date | None = None
    cnpj: str = ""
    emitente: str = ""
    valor: Decimal | None = None
    erro: str = ""

    @property
    def lida(self):
        return self.valor is not None and self.emissao is not None


# ---------- leitura ----------

def _texto(no, caminho):
    alvo = no.find(caminho)
    return (alvo.text or "").strip() if alvo is not None and alvo.text else ""


def ler_xml(conteudo: bytes) -> DadosNota:
    dados = DadosNota("XML")
    try:
        raiz = ElementTree.fromstring(conteudo)
    except Exception as exc:  # noqa: BLE001 - XML malformado ou com construções proibidas
        dados.erro = f"XML inválido ({exc.__class__.__name__})."
        return dados
    inf = raiz.find(f".//{NS}infNFe")
    if inf is None:
        dados.erro = "Não é um XML de NF-e (sem infNFe). NFS-e e outros modelos: associe à mão."
        return dados
    dados.chave = re.sub(r"\D", "", inf.get("Id", ""))[-44:]
    dados.numero = _texto(inf, f"{NS}ide/{NS}nNF")
    dados.serie = _texto(inf, f"{NS}ide/{NS}serie")
    emissao = _texto(inf, f"{NS}ide/{NS}dhEmi") or _texto(inf, f"{NS}ide/{NS}dEmi")
    try:
        dados.emissao = datetime.fromisoformat(emissao[:10]).date() if emissao else None
    except ValueError:
        dados.emissao = None
    dados.cnpj = _texto(inf, f"{NS}emit/{NS}CNPJ") or _texto(inf, f"{NS}emit/{NS}CPF")
    dados.emitente = _texto(inf, f"{NS}emit/{NS}xFant") or _texto(inf, f"{NS}emit/{NS}xNome")
    try:
        dados.valor = Decimal(_texto(inf, f"{NS}total/{NS}ICMSTot/{NS}vNF"))
    except InvalidOperation:
        dados.valor = None
    if not dados.lida:
        dados.erro = "XML sem valor total (vNF) ou data de emissão."
    return dados


def ler_pdf(conteudo: bytes) -> DadosNota:
    dados = DadosNota("PDF")
    try:
        with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
            texto = "\n".join((p.extract_text() or "") for p in pdf.pages[:3])
    except Exception as exc:  # noqa: BLE001
        dados.erro = f"PDF ilegível ({exc.__class__.__name__})."
        return dados
    if not texto.strip():
        dados.erro = "PDF sem texto (imagem): associe à mão."
        return dados
    return dados_do_texto_danfe(texto)


def dados_do_texto_danfe(texto: str) -> DadosNota:
    dados = DadosNota("PDF")
    compacto = re.sub(r"[ .]", "", texto)
    if m := re.search(r"(\d{44})", compacto):
        dados.chave = m.group(1)
        dados.numero = str(int(dados.chave[25:34]))
        dados.serie = str(int(dados.chave[22:25]))
        dados.cnpj = dados.chave[6:20]
    if m := re.search(r"VALOR\s+TOTAL\s+DA\s+NOTA[^\d]{0,40}([\d.]+,\d{2})", texto, re.I):
        dados.valor = Decimal(m.group(1).replace(".", "").replace(",", "."))
    if m := re.search(r"DATA\s+DA\s+EMISS[ÃA]O[^\d]{0,40}(\d{2}/\d{2}/\d{4})", texto, re.I):
        d, mth, a = m.group(1).split("/")
        dados.emissao = date(int(a), int(mth), int(d))
    if m := re.search(r"RECEBEMOS\s+DE\s+(.+?)\s+OS\s+PRODUTOS", texto, re.I | re.S):
        dados.emitente = " ".join(m.group(1).split())[:200]
    if not dados.lida:
        dados.erro = "Não encontrei valor total e data de emissão no PDF: associe à mão."
    return dados


def _arquivos_do_envio(arquivo_enviado):
    """Arquivo único (.xml/.pdf) ou .zip com vários. Devolve [(nome, bytes)]."""
    nome = Path(arquivo_enviado.name).name
    conteudo = b"".join(arquivo_enviado.chunks()) if hasattr(arquivo_enviado, "chunks") else Path(arquivo_enviado).read_bytes()
    if nome.lower().endswith(".zip"):
        try:
            pacote = zipfile.ZipFile(io.BytesIO(conteudo))
        except zipfile.BadZipFile as exc:
            raise arquivos.ArquivoRecusado(f"{nome}: .zip inválido.") from exc
        itens = [i for i in pacote.infolist() if not i.is_dir() and Path(i.filename).suffix.lower() in (".xml", ".pdf")]
        if len(itens) > MAX_ARQUIVOS_ZIP:
            raise arquivos.ArquivoRecusado(f"{nome}: mais de {MAX_ARQUIVOS_ZIP} notas no .zip.")
        saida = []
        for i in itens:
            if i.file_size > MAX_BYTES_ENTRADA_ZIP:
                continue
            saida.append((Path(i.filename).name, pacote.read(i)))
        return saida
    return [(nome, conteudo)]


# ---------- lote ----------

def importar(enviados, competencia: Competencia, *, usuario="", ip=None, maquina="") -> Importacao:
    """Lê e guarda as notas enviadas (arquivos soltos ou .zip) e tenta casar com os lançamentos."""
    if competencia.fechada:
        raise arquivos.ArquivoRecusado(f"A competência {competencia} está FECHADA.")
    itens = []
    for enviado in enviados:
        ext = Path(enviado.name).suffix.lower()
        if ext not in EXTENSOES:
            raise arquivos.ArquivoRecusado(f"{enviado.name}: envie XML, PDF ou um .zip com as notas.")
        itens += _arquivos_do_envio(enviado)
    if not itens:
        raise arquivos.ArquivoRecusado("Nenhuma nota (XML ou PDF) encontrada nos arquivos enviados.")
    # A mesma nota em XML e PDF (mesmo nome de arquivo): vale o XML, o PDF nem é lido.
    com_xml = {Path(n).stem.lower() for n, _ in itens if n.lower().endswith(".xml")}
    pdfs_com_xml = sum(1 for n, _ in itens if n.lower().endswith(".pdf") and Path(n).stem.lower() in com_xml)
    itens = [(n, c) for n, c in itens if not (n.lower().endswith(".pdf") and Path(n).stem.lower() in com_xml)]
    itens.sort(key=lambda i: not i[0].lower().endswith(".xml"))

    contagem = {"lidas": 0, "nao_lidas": 0, "repetidas": 0, "pdf_com_xml": pdfs_com_xml}
    with transaction.atomic():
        ultima = Importacao.objects.filter(tipo=Importacao.Tipo.NF, competencia=competencia).aggregate(v=Max("versao"))
        imp = Importacao.objects.create(tipo=Importacao.Tipo.NF, competencia=competencia,
                                        versao=(ultima["v"] or 0) + 1, origem=Importacao.Origem.UPLOAD,
                                        status=Importacao.Status.OK, ativo=False, usuario=usuario, ip=ip,
                                        maquina=maquina, arquivo_nome_original=f"{len(itens)} arquivo(s)")
        for nome, conteudo in itens:
            ext = Path(nome).suffix.lower()
            inicio = conteudo.lstrip(BOM_E_ESPACOS) if ext == ".xml" else conteudo
            if not inicio.startswith(EXTENSOES[ext]):
                contagem["nao_lidas"] += 1
                continue
            hash_arquivo, caminho = arquivos.guardar(_Pedacos(conteudo), nome,
                                                     subpasta="notas", extensoes={ext},
                                                     assinatura=(b"<", b"\xef\xbb\xbf", b" ", b"\t", b"\r", b"\n",
                                                                 b"%PDF-"),
                                                     descricao="uma nota fiscal")
            dados = ler_xml(conteudo) if ext == ".xml" else ler_pdf(conteudo)
            existente = NotaFiscal.objects.filter(hash_sha256=hash_arquivo).first() or (
                NotaFiscal.objects.filter(chave_acesso=dados.chave).first() if dados.chave else None)
            if existente:
                contagem["repetidas"] += 1
                continue
            NotaFiscal.objects.create(
                importacao=imp, formato=dados.formato, nome_original=nome[:255], hash_sha256=hash_arquivo,
                chave_acesso=dados.chave or None, cnpj_emitente=dados.cnpj[:14], nome_emitente=dados.emitente[:200],
                numero=dados.numero[:20], serie=dados.serie[:5], data_emissao=dados.emissao, valor_total=dados.valor,
                arquivo_caminho=str(caminho), lida=dados.lida, erro=dados.erro)
            contagem["lidas" if dados.lida else "nao_lidas"] += 1
        imp.resumo = contagem
        imp.save(update_fields=["resumo"])
    casadas = casar_abertas(usuario=usuario or "automático")
    imp.resumo = contagem | {"casadas": casadas}
    imp.save(update_fields=["resumo"])
    auditoria.registrar(None, "IMPORTACAO", "OK", competencia=competencia, importacao=imp, usuario=usuario, ip=ip,
                        detalhe=imp.resumo)
    return imp


class _Pedacos:
    """Adapta bytes já em memória ao formato que arquivos.guardar espera (.chunks())."""

    def __init__(self, conteudo):
        self.conteudo = conteudo

    def chunks(self):
        yield self.conteudo


# ---------- casamento ----------

def _palavras(texto):
    return {p for p in re.split(r"[^a-z0-9]+", normalizar_texto(texto)) if len(p) >= 3 and p not in PALAVRAS_GENERICAS}


def _semelhanca(nota, lanc):
    """Palavras em comum entre emitente e descrição da fatura. O início do CNPJ do emitente aparecendo na descrição
    (ex.: "ZP ARTE ACRI23914" × CNPJ 23.914.076/…) também conta como nome batendo."""
    digitos = re.sub(r"\D", "", lanc.descricao)
    if len(nota.cnpj_emitente) >= 8 and len(digitos) >= 5 and nota.cnpj_emitente[:5] in digitos:
        return 1.0
    a, b = _palavras(nota.nome_emitente), _palavras(lanc.descricao)
    return len(a & b) / len(a | b) if a and b else 0


def _mesmo_numero(a, b) -> bool:
    a, b = re.sub(r"\D", "", a or "").lstrip("0"), re.sub(r"\D", "", b or "").lstrip("0")
    return bool(a) and a == b


def lancamentos_pendentes(competencia):
    fatura = competencia.importacoes.filter(tipo=Importacao.Tipo.FATURA, ativo=True).first()
    if fatura is None:
        return []
    anexados = set(Anexo.objects.filter(ativo=True).values_list("chave", flat=True))
    lancs = (FaturaLanc.objects.filter(importacao=fatura, cartao__escopo=Cartao.Escopo.FASE2_NF, valor__gt=0)
             .select_related("cartao").order_by("ordem"))
    return [l for l in lancs if chave_lancamento(l) not in anexados]


def _candidatos(competencia):
    """Pares (lançamento sem nota × nota lida) com o mesmo valor, em níveis de confiança:
    0 = número da NF informado no controle de compras; 1 = valor, data (JANELA_NOTA_DIAS) e nome do emitente;
    2 = valor e data, sem o nome; 3 = só o valor (emissão a até JANELA_VALOR_DIAS). Os níveis 2 e 3 casam com
    ressalva. Ordem: nível, menor distância de datas, maior semelhança, ordem na fatura, nota mais antiga."""
    pendentes = lancamentos_pendentes(competencia)
    if not pendentes:
        return [], []
    usadas_a_vista = set(Anexo.objects.filter(ativo=True, nota__isnull=False).values_list("nota_id", flat=True))
    # Casamento automático que a pessoa removeu não volta sozinho.
    recusados = set(Anexo.objects.filter(ativo=False, automatico=True, nota__isnull=False)
                    .values_list("nota_id", "chave"))
    # Controle de compras: o número da NF registrado para a compra confirma a nota e aceita o total da compra
    # quando ele difere do valor na fatura (frete, por exemplo).
    linhas_controle, usadas_c, do_controle = controle.linhas_ativas(), set(), {}
    for l in pendentes:
        c, _ = controle.encontrar(l, linhas_controle, usadas_c)
        if c is not None:
            usadas_c.add(c.pk)
            if c.nf_numero:
                do_controle[l.pk] = c
    chaves = {l.pk: chave_lancamento(l) for l in pendentes}
    pares = []
    for nota in NotaFiscal.objects.filter(lida=True):
        for l in pendentes:
            n = l.parcela_total or 1
            if (n == 1 and nota.pk in usadas_a_vista) or (nota.pk, chaves[l.pk]) in recusados:
                continue
            dias = abs((nota.data_emissao - l.data).days)
            valor_bate = (nota.valor_total == l.valor if n == 1
                          else abs(nota.valor_total - l.valor * n) <= TOLERANCIA_PARCELA * n)
            c = do_controle.get(l.pk)
            if (c is not None and dias <= JANELA_VALOR_DIAS and _mesmo_numero(nota.numero, c.nf_numero)
                    and (valor_bate or nota.valor_total == c.total_compra)):
                nivel, sim = 0, 1.0
            elif not valor_bate or dias > JANELA_VALOR_DIAS:
                continue
            elif dias > JANELA_NOTA_DIAS:
                nivel, sim = 3, _semelhanca(nota, l)
            else:
                sim = _semelhanca(nota, l)
                nivel = 1 if sim > 0 else 2
            pares.append((nivel, dias, -sim, l.ordem, nota.pk, l, nota))
    return sorted(pares, key=lambda p: p[:5]), pendentes


def _escolher(pares):
    """Cada lançamento recebe no máximo uma nota; nota à vista vale para um lançamento só."""
    escolhidos, usados_l, usadas_n = [], set(), set()
    for nivel, dias, _, _, _, l, nota in pares:
        if l.pk in usados_l or ((l.parcela_total or 1) == 1 and nota.pk in usadas_n):
            continue
        usados_l.add(l.pk)
        if (l.parcela_total or 1) == 1:
            usadas_n.add(nota.pk)
        escolhidos.append((l, nota, nivel, dias))
    return escolhidos


def _ressalva(nivel, dias):
    if nivel == 2:
        return "nota casada pelo valor e data; o emitente não aparece na fatura (conferir)"
    if nivel == 3:
        return f"nota casada só pelo valor; emissão a {dias} dias da compra (conferir)"
    return ""


def casar(competencia, *, usuario="automático") -> int:
    """O valor manda (pedido de 16/10/2026): cada lançamento sem nota recebe a nota de mesmo valor. Com o número
    da NF (controle) ou o nome do emitente batendo, fica conciliado; só pelo valor, conciliado com ressalva — o
    casamento pode ser desfeito na tela e não volta sozinho. Devolve quantos casou."""
    pares, pendentes = _candidatos(competencia)
    casadas = 0
    with transaction.atomic():
        for l, nota, nivel, dias in _escolher(pares):
            _anexar(nota, l, competencia, usuario, automatico=True, ressalva=_ressalva(nivel, dias))
            casadas += 1
    if casadas:
        cartoes = list(Cartao.objects.filter(pk__in={l.cartao_id for l in pendentes}))
        motor.executar(competencia, cartoes, motivo="notas fiscais", usuario=usuario)
    log.info("notas casadas", extra={"competencia": str(competencia), "casadas": casadas})
    return casadas


def casar_abertas(*, usuario="automático") -> int:
    """Notas valem para qualquer banco: tenta em todas as competências abertas com fatura em uso."""
    total = 0
    for comp in (Competencia.objects.filter(status=Competencia.Status.ABERTA, importacoes__tipo=Importacao.Tipo.FATURA,
                                            importacoes__ativo=True).distinct()):
        total += casar(comp, usuario=usuario)
    return total


def _anexar(nota, lanc, competencia, usuario, automatico, ip=None, ressalva=""):
    mime = "application/xml" if nota.formato == NotaFiscal.Formato.XML else "application/pdf"
    rotulo = f"NF {nota.numero or '?'} {nota.nome_emitente or nota.nome_original}"[:255]
    return Anexo.objects.create(competencia=competencia, chave=chave_lancamento(lanc), arquivo_caminho=nota.arquivo_caminho,
                                nome_original=rotulo, mime=mime, hash_sha256=nota.hash_sha256 or "", tamanho=0,
                                usuario=usuario, ip=ip, nota=nota, automatico=automatico, ressalva=ressalva[:200])


def associar(nota: NotaFiscal, resultado: Resultado, *, usuario="", ip=None):
    """Associação feita pela pessoa (nota que não casou sozinha)."""
    lanc = resultado.fatura_lanc
    anexo = _anexar(nota, lanc, resultado.competencia, usuario, automatico=False, ip=ip)
    motor.executar(resultado.competencia, [lanc.cartao], motivo="nota fiscal associada", usuario=usuario)
    return anexo


def notas_sem_lancamento():
    """Notas lidas ou não que ainda não foram ligadas a nenhum lançamento."""
    return NotaFiscal.objects.exclude(anexos__ativo=True).order_by("-data_emissao", "nome_original")
