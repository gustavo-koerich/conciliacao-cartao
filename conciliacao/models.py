"""Modelo de dados da conciliação (schema "conciliacao", tabelas conc_*).

Nada é apagado: versões antigas ficam inativas e os relacionamentos usam PROTECT.
Valores monetários em Decimal (2 casas); nunca float.
"""
import uuid
from datetime import timezone as dt_timezone

from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q


class ContaCartao(models.Model):
    """Um banco/conta de cartão corporativo. Cada um tem a própria fatura, fechamento e cartões."""

    class Leitor(models.TextChoices):
        AILOS_PDF = "AILOS_PDF", "Ailos (PDF da fatura, texto ou imagem)"
        BB = "BB", "Banco do Brasil (PDF da fatura ou Excel exportado)"

    nome = models.CharField(max_length=80, unique=True, help_text="Ex.: Ailos Mastercard Empresas Pro")
    banco = models.CharField(max_length=40, help_text="Nome curto que aparece nas telas. Ex.: Ailos, BB")
    slug = models.SlugField(max_length=30, unique=True, help_text="Identificador nos endereços. Ex.: ailos, bb")
    leitor = models.CharField(max_length=12, choices=Leitor.choices)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "conc_conta_cartao"
        ordering = ["nome"]
        verbose_name = "conta de cartão (banco)"
        verbose_name_plural = "contas de cartão (bancos)"

    def __str__(self):
        return self.banco


def conta_padrao():
    """Conta usada quando nenhuma é informada (dados anteriores ao cadastro de bancos: Ailos)."""
    conta, _ = ContaCartao.objects.get_or_create(
        slug="ailos", defaults={"nome": "Ailos Mastercard Empresas Pro", "banco": "Ailos",
                                "leitor": ContaCartao.Leitor.AILOS_PDF})
    return conta.pk


class Competencia(models.Model):
    class Status(models.TextChoices):
        ABERTA = "ABERTA", "Aberta"
        FECHADA = "FECHADA", "Fechada"

    conta = models.ForeignKey(ContaCartao, on_delete=models.PROTECT, default=conta_padrao,
                              related_name="competencias")
    # Uma por conta e mês; ano_mes = mês do fechamento da fatura daquela conta.
    ano_mes = models.CharField(max_length=7, validators=[RegexValidator(r"^\d{4}-(0[1-9]|1[0-2])$", "Use AAAA-MM.")])
    periodo_ini = models.DateField()
    periodo_fim = models.DateField()
    fechamento = models.DateField()
    vencimento = models.DateField()
    total_fatura = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ABERTA)
    fechada_em = models.DateTimeField(null=True, blank=True)
    fechada_por = models.CharField(max_length=150, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "conc_competencia"
        ordering = ["-ano_mes", "conta"]
        verbose_name = "competência"
        constraints = [
            models.CheckConstraint(condition=Q(periodo_ini__lte=models.F("periodo_fim")), name="conc_competencia_periodo"),
            models.UniqueConstraint(fields=["conta", "ano_mes"], name="conc_competencia_conta_mes"),
        ]

    def __str__(self):
        return f"{self.conta.banco} {self.ano_mes}"

    @property
    def codigo(self) -> str:
        """Identificador nos endereços: "<conta>-<AAAA-MM>", ex.: ailos-2026-08."""
        return f"{self.conta.slug}-{self.ano_mes}"

    @classmethod
    def por_codigo(cls, codigo: str):
        return cls.objects.select_related("conta").get(conta__slug=codigo[:-8], ano_mes=codigo[-7:])

    @property
    def fechada(self) -> bool:
        return self.status == self.Status.FECHADA


class Representante(models.Model):
    nome = models.CharField(max_length=120)
    email = models.EmailField(blank=True)
    # Vazio até ser cadastrado; quando preenchido, é único.
    sheet_id = models.CharField(max_length=120, null=True, blank=True, unique=True)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "conc_representante"
        ordering = ["nome"]

    def __str__(self):
        return self.nome


class Cartao(models.Model):
    class Escopo(models.TextChoices):
        CONCILIAR_FORMULARIO = "CONCILIAR_FORMULARIO", "Conciliar com formulário"
        FASE2_NF = "FASE2_NF", "Notas fiscais (fase 2)"
        FORA_ESCOPO = "FORA_ESCOPO", "Fora do escopo"

    # A chave é conta + final do cartão, nunca o nome do portador (bancos diferentes podem repetir o final).
    conta = models.ForeignKey(ContaCartao, on_delete=models.PROTECT, default=conta_padrao, related_name="cartoes")
    final = models.CharField(max_length=4, validators=[RegexValidator(r"^\d{4}$", "4 dígitos.")])
    nome_fatura = models.CharField("nome na fatura", max_length=120)
    representante = models.ForeignKey(Representante, on_delete=models.PROTECT, null=True, blank=True,
                                      related_name="cartoes")
    escopo = models.CharField(max_length=24, choices=Escopo.choices, default=Escopo.CONCILIAR_FORMULARIO)
    # Criado automaticamente pela leitura da fatura; aparece como aviso até alguém completar o cadastro.
    pendente_cadastro = models.BooleanField(default=False)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "conc_cartao"
        ordering = ["conta", "final"]
        verbose_name = "cartão"
        verbose_name_plural = "cartões"
        constraints = [models.UniqueConstraint(fields=["conta", "final"], name="conc_cartao_conta_final")]

    def __str__(self):
        return f"{self.final} · {self.nome_fatura}"


class Importacao(models.Model):
    class Tipo(models.TextChoices):
        FATURA = "FATURA", "Fatura"
        PLANILHA = "PLANILHA", "Planilha do formulário"
        RECIBOS = "RECIBOS", "Recibos"
        NF = "NF", "Notas fiscais"
        BASE = "BASE", "Base geral de despesas"
        CONTROLE = "CONTROLE", "Controle de compras (cartão de notas fiscais)"

    class Origem(models.TextChoices):
        UPLOAD = "UPLOAD", "Upload"
        GOOGLE_SHEETS = "GOOGLE_SHEETS", "Google Sheets"
        GOOGLE_DRIVE = "GOOGLE_DRIVE", "Google Drive"
        CSV_OFX = "CSV_OFX", "Arquivo do banco (Excel/CSV/OFX)"

    class Status(models.TextChoices):
        PROCESSANDO = "PROCESSANDO", "Processando"
        OK = "OK", "OK"
        ERRO = "ERRO", "Erro"

    class Metodo(models.TextChoices):
        CSV_OFX = "CSV_OFX", "Arquivo do banco (Excel/CSV/OFX)"
        TEXTO = "TEXTO", "Texto do PDF"
        OCR = "OCR", "OCR"
        IA = "IA", "IA (reserva)"

    tipo = models.CharField(max_length=10, choices=Tipo.choices)
    competencia = models.ForeignKey(Competencia, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="importacoes")
    representante = models.ForeignKey(Representante, on_delete=models.PROTECT, null=True, blank=True,
                                      related_name="importacoes")
    # Fatura: banco escolhido no envio (a competência só é conhecida depois da leitura).
    conta = models.ForeignKey(ContaCartao, on_delete=models.PROTECT, null=True, blank=True, related_name="importacoes")
    # A fatura só revela a competência depois de lida: competência e versão podem ficar vazias enquanto processa
    # ou quando a leitura falha antes do resumo.
    versao = models.PositiveIntegerField(null=True, blank=True)
    origem = models.CharField(max_length=16, choices=Origem.choices)
    arquivo_caminho = models.CharField(max_length=500, blank=True)
    arquivo_nome_original = models.CharField(max_length=255, blank=True)
    hash_sha256 = models.CharField(max_length=64, blank=True)
    # Só vira ativa quando a pessoa confirma a prévia ("Substituir e reconciliar").
    ativo = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PROCESSANDO)
    resumo = models.JSONField(default=dict, blank=True)
    metodo_leitura = models.CharField(max_length=8, choices=Metodo.choices, blank=True)
    usuario = models.CharField(max_length=150, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    maquina = models.CharField(max_length=255, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    ativado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "conc_importacao"
        ordering = ["-criado_em"]
        verbose_name = "importação"
        verbose_name_plural = "importações"
        constraints = [
            # No máximo uma versão ativa por (tipo, competência, representante). NULL conta como igual.
            models.UniqueConstraint(fields=["tipo", "competencia", "representante"], condition=Q(ativo=True),
                                    nulls_distinct=False, name="conc_importacao_uma_ativa"),
            models.UniqueConstraint(fields=["tipo", "competencia", "representante", "versao"],
                                    condition=Q(versao__isnull=False),
                                    nulls_distinct=False, name="conc_importacao_versao"),
            models.CheckConstraint(condition=~Q(tipo__in=["FATURA", "NF"]) | Q(competencia__isnull=False)
                                   | ~Q(status="OK"),
                                   name="conc_importacao_competencia_obrigatoria"),
            models.CheckConstraint(condition=~Q(status="OK") | Q(versao__isnull=False),
                                   name="conc_importacao_ok_tem_versao"),
            models.CheckConstraint(condition=~Q(tipo__in=["PLANILHA", "RECIBOS"]) | Q(representante__isnull=False),
                                   name="conc_importacao_representante_obrigatorio"),
            models.CheckConstraint(condition=Q(ativo=False) | Q(status="OK"), name="conc_importacao_ativa_so_ok"),
        ]
        indexes = [models.Index(fields=["hash_sha256"], name="conc_importacao_hash")]

    def __str__(self):
        alvo = self.competencia or self.representante or "-"
        return f"{self.get_tipo_display()} {alvo} v{self.versao}"


class FaturaSecao(models.Model):
    """Uma seção da fatura (um portador, ou CONTA) dentro de uma importação, com a conferência do total."""

    class Status(models.TextChoices):
        OK = "OK", "Fechou"
        ERRO_LEITURA = "ERRO_LEITURA", "Erro na leitura"

    importacao = models.ForeignKey(Importacao, on_delete=models.PROTECT, related_name="secoes")
    cartao = models.ForeignKey(Cartao, on_delete=models.PROTECT, null=True, blank=True,
                               help_text="Vazio = seção Movimentações da conta.")
    nome_portador = models.CharField(max_length=120, blank=True)
    total_declarado = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    total_lido = models.DecimalField(max_digits=14, decimal_places=2)
    diferenca = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    pagina_ini = models.PositiveSmallIntegerField(null=True, blank=True)
    pagina_fim = models.PositiveSmallIntegerField(null=True, blank=True)
    metodo_leitura = models.CharField(max_length=8, choices=Importacao.Metodo.choices, blank=True)
    status = models.CharField(max_length=14, choices=Status.choices)

    class Meta:
        db_table = "conc_fatura_secao"
        ordering = ["importacao", "pagina_ini"]
        verbose_name = "seção da fatura"
        verbose_name_plural = "seções da fatura"
        constraints = [
            models.UniqueConstraint(fields=["importacao", "cartao"], nulls_distinct=False, name="conc_fatura_secao_unica"),
        ]

    def __str__(self):
        return f"{self.cartao or 'CONTA'} ({self.get_status_display()})"


class FaturaLanc(models.Model):
    class Secao(models.TextChoices):
        PORTADOR = "PORTADOR", "Portador"
        CONTA = "CONTA", "Movimentações da conta"

    class Natureza(models.TextChoices):
        COMPRA = "COMPRA", "Compra"
        CREDITO = "CREDITO", "Estorno/crédito"
        ENCARGO = "ENCARGO", "Encargo da conta"
        SALDO_ANTERIOR = "SALDO_ANTERIOR", "Saldo anterior"
        PAGAMENTO = "PAGAMENTO", "Pagamento"

    importacao = models.ForeignKey(Importacao, on_delete=models.PROTECT, related_name="lancamentos")
    cartao = models.ForeignKey(Cartao, on_delete=models.PROTECT, null=True, blank=True, related_name="lancamentos")
    secao = models.CharField(max_length=10, choices=Secao.choices)
    # Saldo anterior e pagamento ficam guardados, mas fora da soma de encargos.
    natureza = models.CharField(max_length=16, choices=Natureza.choices)
    data = models.DateField()
    descricao = models.CharField(max_length=200)
    cidade = models.CharField(max_length=80, blank=True)
    valor = models.DecimalField(max_digits=12, decimal_places=2)
    parcela_atual = models.PositiveSmallIntegerField(null=True, blank=True)
    parcela_total = models.PositiveSmallIntegerField(null=True, blank=True)
    moeda = models.CharField(max_length=3, default="BRL")
    valor_moeda = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cotacao = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    ordem = models.PositiveIntegerField(help_text="Posição na fatura.")
    pagina = models.PositiveSmallIntegerField(null=True, blank=True)
    # Lançamentos idênticos legítimos (dois estacionamentos de R$ 10 no mesmo dia) são diferenciados aqui.
    n_ocorrencia = models.PositiveSmallIntegerField(default=1)

    class Meta:
        db_table = "conc_fatura_lanc"
        ordering = ["importacao", "ordem"]
        verbose_name = "lançamento da fatura"
        verbose_name_plural = "lançamentos da fatura"
        constraints = [
            models.UniqueConstraint(fields=["importacao", "cartao", "data", "descricao", "valor", "n_ocorrencia"],
                                    nulls_distinct=False, name="conc_fatura_lanc_chave"),
            models.CheckConstraint(
                condition=(Q(parcela_atual__isnull=True) & Q(parcela_total__isnull=True))
                | Q(parcela_atual__gte=1, parcela_atual__lte=models.F("parcela_total")),
                name="conc_fatura_lanc_parcela"),
            models.CheckConstraint(condition=Q(secao="CONTA") | Q(cartao__isnull=False),
                                   name="conc_fatura_lanc_portador_tem_cartao"),
        ]

    def __str__(self):
        return f"{self.data:%d/%m} {self.descricao} {self.valor}"


class FormDespesa(models.Model):
    """Uma resposta do Google Forms numa versão da planilha. Chave natural: (representante, carimbo)."""

    importacao = models.ForeignKey(Importacao, on_delete=models.PROTECT, related_name="despesas")
    representante = models.ForeignKey(Representante, on_delete=models.PROTECT, related_name="despesas")
    linha_planilha = models.PositiveIntegerField()
    carimbo = models.DateTimeField()
    data_informada = models.DateField(null=True, blank=True)
    data_corrigida = models.DateField(null=True, blank=True)
    despesa = models.CharField(max_length=60, blank=True)
    descricao = models.TextField(blank=True)
    finalidade = models.TextField(blank=True)
    pagamento = models.CharField(max_length=60, blank=True)
    medico_original = models.CharField(max_length=200, blank=True)
    medico_canonico = models.CharField(max_length=200, blank=True)
    paciente = models.CharField(max_length=200, blank=True)
    cidade = models.CharField(max_length=120, blank=True)
    valor = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    valor_original = models.CharField(max_length=200, blank=True, help_text="Texto como veio da planilha.")
    link_comprovante = models.TextField(blank=True)
    data_suspeita = models.BooleanField(default=False)
    duplicado = models.BooleanField(default=False)
    valor_invalido = models.BooleanField(default=False)
    pagamento_outro_cartao = models.BooleanField(default=False)
    moeda_estrangeira = models.BooleanField(default=False)

    class Meta:
        db_table = "conc_form_despesa"
        ordering = ["importacao", "carimbo"]
        verbose_name = "despesa do formulário"
        verbose_name_plural = "despesas do formulário"
        constraints = [
            models.UniqueConstraint(fields=["importacao", "carimbo"], name="conc_form_despesa_chave"),
        ]
        indexes = [models.Index(fields=["representante", "carimbo"], name="conc_form_despesa_natural")]

    def __str__(self):
        return f"{self.representante} {self.carimbo:%d/%m/%Y %H:%M}"

    @property
    def data_efetiva(self):
        return self.data_corrigida or self.data_informada


class Comprovante(models.Model):
    """Recibo baixado do Drive. Ligado à despesa pela chave natural (representante, carimbo), não pela linha
    de uma versão da planilha: assim trocar a planilha não perde os recibos já baixados."""

    class Status(models.TextChoices):
        OK = "OK", "OK"
        SEM_LINK_VALIDO = "SEM_LINK_VALIDO", "Sem link válido"
        FALHA_DOWNLOAD = "FALHA_DOWNLOAD", "Falha no download"

    importacao = models.ForeignKey(Importacao, on_delete=models.PROTECT, related_name="comprovantes")
    representante = models.ForeignKey(Representante, on_delete=models.PROTECT, related_name="comprovantes")
    carimbo = models.DateTimeField()
    # Uma resposta pode ter mais de um arquivo anexado.
    ordem = models.PositiveSmallIntegerField(default=1)
    url = models.TextField(blank=True)
    caminho_local = models.CharField(max_length=500, blank=True)
    nome_original = models.CharField(max_length=255, blank=True)
    mime = models.CharField(max_length=100, blank=True)
    hash_sha256 = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices)
    erro = models.TextField(blank=True)
    baixado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "conc_comprovante"
        ordering = ["importacao", "carimbo", "ordem"]
        constraints = [
            models.UniqueConstraint(fields=["importacao", "carimbo", "ordem"], name="conc_comprovante_chave"),
        ]
        indexes = [models.Index(fields=["representante", "carimbo"], name="conc_comprovante_natural")]

    def __str__(self):
        return f"{self.representante} {self.carimbo:%d/%m/%Y %H:%M} #{self.ordem}"


class Execucao(models.Model):
    class Status(models.TextChoices):
        PROCESSANDO = "PROCESSANDO", "Processando"
        OK = "OK", "OK"
        ERRO = "ERRO", "Erro"

    competencia = models.ForeignKey(Competencia, on_delete=models.PROTECT, related_name="execucoes")
    # Lista de finais de cartão; vazio = todos os cartões da competência.
    escopo = models.JSONField(default=list, blank=True)
    motivo = models.CharField(max_length=40, blank=True, help_text="O que disparou: fatura, planilha, restauração…")
    inicio = models.DateTimeField(auto_now_add=True)
    fim = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PROCESSANDO)
    parametros = models.JSONField(default=dict, help_text="Cópia de conc_parametro no momento da execução.")
    resumo = models.JSONField(default=dict, blank=True, help_text="Totais por status e resultado da trava.")
    usuario = models.CharField(max_length=150, blank=True)

    class Meta:
        db_table = "conc_execucao"
        ordering = ["-inicio"]
        verbose_name = "execução"
        verbose_name_plural = "execuções"

    def __str__(self):
        return f"{self.competencia} {self.inicio:%d/%m %H:%M} {self.status}"


class Resultado(models.Model):
    class Status(models.TextChoices):
        CONCILIADO = "CONCILIADO", "Conciliado"
        CONCILIADO_RESSALVA = "CONCILIADO_RESSALVA", "Conciliado com ressalva"
        SEM_FORMULARIO = "SEM_FORMULARIO", "Sem formulário"
        SEM_NOTA = "SEM_NOTA", "Sem nota fiscal"
        SEM_COBRANCA = "SEM_COBRANCA", "Sem cobrança"
        PENDENTE_ANALISE = "PENDENTE_ANALISE", "Pendente de análise"
        AGUARDANDO_PLANILHA = "AGUARDANDO_PLANILHA", "Aguardando planilha"
        AGUARDANDO_APROVACAO = "AGUARDANDO_APROVACAO", "Respondido, aguardando aprovação"
        DUPLICADO_FORM = "DUPLICADO_FORM", "Duplicado no formulário"
        REEMBOLSO = "REEMBOLSO", "Reembolso"
        ESTORNO_COMPENSADO = "ESTORNO_COMPENSADO", "Estorno compensado"
        ENCARGOS_CONTA = "ENCARGOS_CONTA", "Encargos da conta"
        FORA_ESCOPO = "FORA_ESCOPO", "Fora do escopo"

    class Regra(models.TextChoices):
        EXATO = "EXATO", "1. Exato"
        JANELA = "JANELA", "2. Janela de datas"
        DATA_CORRIGIDA = "DATA_CORRIGIDA", "3. Data corrigida"
        PARCELADO = "PARCELADO", "4. Parcelado"
        MOEDA = "MOEDA", "5. Moeda estrangeira"
        DIVIDIDA = "DIVIDIDA", "6. Conta dividida"
        ESTORNO = "ESTORNO", "7. Estorno"

    execucao = models.ForeignKey(Execucao, on_delete=models.PROTECT, related_name="resultados")
    competencia = models.ForeignKey(Competencia, on_delete=models.PROTECT, related_name="resultados")
    representante = models.ForeignKey(Representante, on_delete=models.PROTECT, null=True, blank=True)
    cartao = models.ForeignKey(Cartao, on_delete=models.PROTECT, null=True, blank=True)
    fatura_lanc = models.ForeignKey(FaturaLanc, on_delete=models.PROTECT, null=True, blank=True)
    form_despesa = models.ForeignKey(FormDespesa, on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    regra = models.CharField(max_length=16, choices=Regra.choices, blank=True)
    score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    # Conta dividida: as linhas do mesmo casamento compartilham o grupo.
    grupo = models.UUIDField(null=True, blank=True)
    # Valor que entra na trava de fechamento (lado da fatura, uma única vez por grupo; nulo no lado formulário).
    valor_trava = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    sem_comprovante = models.BooleanField(default=False)
    motivo = models.TextField(blank=True)
    # Resultado em vigor para (competência, cartão); a execução seguinte do mesmo escopo o substitui.
    vigente = models.BooleanField(default=True)
    # Ajuste manual reaplicado pelo motor: status_automatico guarda o que o motor tinha decidido.
    status_automatico = models.CharField(max_length=20, blank=True)
    ajuste = models.ForeignKey("AjusteManual", on_delete=models.PROTECT, null=True, blank=True, related_name="+")

    class Meta:
        db_table = "conc_resultado"
        ordering = ["execucao", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(fatura_lanc__isnull=False) | Q(form_despesa__isnull=False),
                                   name="conc_resultado_tem_um_lado"),
        ]
        indexes = [
            models.Index(fields=["competencia", "cartao"], condition=Q(vigente=True), name="conc_resultado_vigente"),
            models.Index(fields=["grupo"], name="conc_resultado_grupo"),
        ]

    def __str__(self):
        return f"{self.get_status_display()} ({self.regra or '-'})"

    @staticmethod
    def novo_grupo():
        return uuid.uuid4()


class VinculoParcela(models.Model):
    """Liga a parcela 02..N à despesa do formulário casada na parcela 01."""

    execucao = models.ForeignKey(Execucao, on_delete=models.PROTECT, related_name="vinculos")
    fatura_lanc = models.ForeignKey(FaturaLanc, on_delete=models.PROTECT, related_name="vinculos")
    parcela_01 = models.ForeignKey(FaturaLanc, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    form_despesa = models.ForeignKey(FormDespesa, on_delete=models.PROTECT, related_name="vinculos_parcela")

    class Meta:
        db_table = "conc_vinculo_parcela"
        verbose_name = "vínculo de parcela"
        verbose_name_plural = "vínculos de parcela"
        constraints = [
            models.UniqueConstraint(fields=["execucao", "fatura_lanc"], name="conc_vinculo_parcela_unico"),
        ]


class Parametro(models.Model):
    """Parâmetros do motor, editáveis no admin. Tipos e padrões ficam em conciliacao/parametros.py."""

    chave = models.CharField(max_length=60, unique=True)
    valor = models.JSONField()
    descricao = models.TextField(blank=True)
    atualizado_em = models.DateTimeField(auto_now=True)
    atualizado_por = models.CharField(max_length=150, blank=True)

    class Meta:
        db_table = "conc_parametro"
        ordering = ["chave"]
        verbose_name = "parâmetro"

    def __str__(self):
        return self.chave


class Auditoria(models.Model):
    class Acao(models.TextChoices):
        IMPORTACAO = "IMPORTACAO", "Importação"
        ATUALIZACAO = "ATUALIZACAO", "Atualização (substituir versão)"
        RESTAURACAO_VERSAO = "RESTAURACAO_VERSAO", "Restauração de versão"
        REPROCESSAMENTO = "REPROCESSAMENTO", "Reprocessamento"
        FECHAMENTO = "FECHAMENTO", "Fechamento de competência"
        ENVIO_PENDENCIAS = "ENVIO_PENDENCIAS", "Envio de pendências"
        BACKUP = "BACKUP", "Backup"
        CADASTRO = "CADASTRO", "Alteração de cadastro/parâmetro"
        AJUSTE_MANUAL = "AJUSTE_MANUAL", "Ajuste manual de situação"
        ANEXO = "ANEXO", "Comprovante anexado manualmente"
        EXPORTACAO = "EXPORTACAO", "Exportação de dados"
        EXCLUSAO_TESTE = "EXCLUSAO_TESTE", "Exclusão de dados de teste"

    class Resultado(models.TextChoices):
        OK = "OK", "OK"
        ERRO = "ERRO", "Erro"
        RECUSADO = "RECUSADO", "Recusado"

    momento = models.DateTimeField(auto_now_add=True)
    acao = models.CharField(max_length=20, choices=Acao.choices)
    competencia = models.ForeignKey(Competencia, on_delete=models.PROTECT, null=True, blank=True)
    representante = models.ForeignKey(Representante, on_delete=models.PROTECT, null=True, blank=True)
    cartao = models.ForeignKey(Cartao, on_delete=models.PROTECT, null=True, blank=True)
    importacao = models.ForeignKey(Importacao, on_delete=models.PROTECT, null=True, blank=True)
    usuario = models.CharField(max_length=150, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    maquina = models.CharField(max_length=255, blank=True)
    resultado = models.CharField(max_length=10, choices=Resultado.choices)
    detalhe = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "conc_auditoria"
        ordering = ["-momento"]
        verbose_name = "registro de auditoria"
        verbose_name_plural = "auditoria"

    def __str__(self):
        return f"{self.momento:%d/%m/%Y %H:%M} {self.get_acao_display()} {self.resultado}"


class Backup(models.Model):
    class Tipo(models.TextChoices):
        BACKUP = "BACKUP", "Backup"
        TESTE_RESTAURACAO = "TESTE_RESTAURACAO", "Teste de restauração"

    class Status(models.TextChoices):
        OK = "OK", "OK"
        ERRO = "ERRO", "Erro"

    momento = models.DateTimeField(auto_now_add=True)
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    arquivo = models.CharField(max_length=500, blank=True)
    tamanho = models.BigIntegerField(null=True, blank=True)
    hash_sha256 = models.CharField(max_length=64, blank=True)
    destino = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=6, choices=Status.choices)
    duracao_seg = models.PositiveIntegerField(null=True, blank=True)
    detalhe = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "conc_backup"
        ordering = ["-momento"]

    def __str__(self):
        return f"{self.momento:%d/%m/%Y %H:%M} {self.get_tipo_display()} {self.status}"


class NotaFiscal(models.Model):
    """Nota fiscal enviada para os cartões de notas fiscais (FASE2_NF): XML (NF-e) de preferência, ou PDF (DANFE).
    O casamento com o lançamento gera um Anexo ligado à nota (uma nota parcelada cobre as N parcelas)."""

    class Formato(models.TextChoices):
        XML = "XML", "XML (NF-e)"
        PDF = "PDF", "PDF (DANFE)"

    importacao = models.ForeignKey(Importacao, on_delete=models.PROTECT, related_name="notas")
    formato = models.CharField(max_length=4, choices=Formato.choices, default=Formato.XML)
    nome_original = models.CharField(max_length=255, blank=True)
    hash_sha256 = models.CharField(max_length=64, unique=True, null=True, blank=True)
    lida = models.BooleanField(default=False, help_text="Valor e data de emissão reconhecidos no arquivo.")
    erro = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True, null=True)
    chave_acesso = models.CharField(max_length=44, null=True, blank=True, unique=True)
    cnpj_emitente = models.CharField(max_length=14, blank=True)
    nome_emitente = models.CharField(max_length=200, blank=True)
    numero = models.CharField(max_length=20, blank=True)
    serie = models.CharField(max_length=5, blank=True)
    data_emissao = models.DateField(null=True, blank=True)
    valor_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    arquivo_caminho = models.CharField(max_length=500, blank=True)
    cartao = models.ForeignKey(Cartao, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        db_table = "conc_nota_fiscal"
        ordering = ["-data_emissao", "id"]
        verbose_name = "nota fiscal"
        verbose_name_plural = "notas fiscais"

    def __str__(self):
        return f"NF {self.numero or '?'} · {self.nome_emitente or self.nome_original}"


def chave_lancamento(lanc) -> str:
    """Identidade de um lançamento da fatura que sobrevive a uma nova versão da fatura (chave natural)."""
    final = lanc.cartao.final if lanc.cartao_id else "CONTA"
    return f"F|{final}|{lanc.data:%Y-%m-%d}|{lanc.descricao}|{lanc.valor}|{lanc.n_ocorrencia}"


def chave_despesa(despesa) -> str:
    """Identidade de uma resposta do formulário que sobrevive a uma nova versão da planilha."""
    return f"D|{despesa.representante_id}|{despesa.carimbo.astimezone(dt_timezone.utc):%Y-%m-%dT%H:%M:%S.%f}"


def chave_resultado(resultado) -> str:
    """O ajuste manual se prende ao lançamento da fatura; sem lançamento, à resposta do formulário."""
    if resultado.fatura_lanc_id:
        return chave_lancamento(resultado.fatura_lanc)
    return chave_despesa(resultado.form_despesa)


class AjusteManual(models.Model):
    """Situação definida por uma pessoa. O motor reaplica a cada recálculo; nada é apagado (ativo=False)."""

    competencia = models.ForeignKey(Competencia, on_delete=models.PROTECT, related_name="ajustes")
    chave = models.CharField(max_length=400)
    status = models.CharField(max_length=20, choices=Resultado.Status.choices)
    observacao = models.TextField(blank=True)
    usuario = models.CharField(max_length=150, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        db_table = "conc_ajuste_manual"
        ordering = ["-criado_em"]
        verbose_name = "ajuste manual"
        verbose_name_plural = "ajustes manuais"
        constraints = [
            models.UniqueConstraint(fields=["competencia", "chave"], condition=Q(ativo=True),
                                    name="conc_ajuste_um_ativo"),
        ]

    def __str__(self):
        return f"{self.get_status_display()} por {self.usuario or '—'}"


class CorrecaoValor(models.Model):
    """Valor que o representante informou errado, corrigido por uma pessoa. O original nunca é apagado (fica na
    resposta e aqui); o motor e a análise passam a usar o corrigido. Presa à resposta (chave_despesa), vale também
    para uma nova versão da planilha. O valor da fatura não se corrige: vem do banco."""

    chave = models.CharField(max_length=400, db_index=True)
    representante = models.ForeignKey(Representante, on_delete=models.PROTECT, related_name="correcoes_valor")
    valor_original = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    valor_original_texto = models.CharField(max_length=200, blank=True)
    valor_corrigido = models.DecimalField(max_digits=12, decimal_places=2)
    motivo = models.TextField()
    usuario = models.CharField(max_length=150, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        db_table = "conc_correcao_valor"
        ordering = ["-criado_em"]
        verbose_name = "correção de valor"
        verbose_name_plural = "correções de valor"
        constraints = [
            models.UniqueConstraint(fields=["chave"], condition=Q(ativo=True), name="conc_correcao_uma_ativa"),
            models.CheckConstraint(condition=Q(valor_corrigido__gt=0), name="conc_correcao_valor_positivo"),
        ]

    def __str__(self):
        return f"{self.valor_original} → {self.valor_corrigido} por {self.usuario or '—'}"


class CorrecaoDespesa(models.Model):
    """Data ou categoria que o representante preencheu errado, corrigida por uma pessoa. Como na correção de valor:
    o original fica (na resposta e aqui), o motor e a análise usam o corrigido, e vale para nova versão da planilha."""

    class Campo(models.TextChoices):
        DATA = "DATA", "Data"
        CATEGORIA = "CATEGORIA", "Categoria"

    chave = models.CharField(max_length=400, db_index=True)
    representante = models.ForeignKey(Representante, on_delete=models.PROTECT, related_name="correcoes_despesa")
    campo = models.CharField(max_length=12, choices=Campo.choices)
    original = models.CharField(max_length=200, blank=True)
    novo = models.CharField(max_length=200)            # data em AAAA-MM-DD; categoria como na lista
    motivo = models.TextField(blank=True)
    usuario = models.CharField(max_length=150, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        db_table = "conc_correcao_despesa"
        ordering = ["-criado_em"]
        verbose_name = "correção de data/categoria"
        verbose_name_plural = "correções de data/categoria"
        constraints = [models.UniqueConstraint(fields=["chave", "campo"], condition=Q(ativo=True),
                                               name="conc_correcao_despesa_uma_ativa")]

    def __str__(self):
        return f"{self.get_campo_display()}: {self.original} → {self.novo}"


class LinkPendencias(models.Model):
    """Link pessoal que o representante recebe (WhatsApp) para responder os gastos da fatura sem formulário.
    O código do link não é guardado: só a impressão (SHA-256). Vale até `expira_em`, pode ser revogado e só mostra
    os lançamentos listados em `chaves` (os pendentes daquele representante naquela fatura, na hora de gerar)."""

    representante = models.ForeignKey(Representante, on_delete=models.PROTECT, related_name="links_pendencias")
    competencia = models.ForeignKey(Competencia, on_delete=models.PROTECT, related_name="links_pendencias")
    token_hash = models.CharField(max_length=64, unique=True)
    chaves = models.JSONField(default=list)                  # chave_lancamento de cada item
    criado_em = models.DateTimeField(auto_now_add=True)
    criado_por = models.CharField(max_length=150, blank=True)
    expira_em = models.DateTimeField()
    revogado_em = models.DateTimeField(null=True, blank=True)
    revogado_por = models.CharField(max_length=150, blank=True)
    aberto_em = models.DateTimeField(null=True, blank=True)
    ultimo_acesso_em = models.DateTimeField(null=True, blank=True)
    acessos = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "conc_link_pendencias"
        ordering = ["-criado_em"]
        verbose_name = "link de pendências"
        verbose_name_plural = "links de pendências"

    @property
    def valido(self):
        from django.utils import timezone
        return self.revogado_em is None and self.expira_em > timezone.now()

    def __str__(self):
        return f"{self.representante} · {self.competencia} · até {self.expira_em:%d/%m/%Y}"


class RespostaPendencia(models.Model):
    """O que o representante respondeu pelo link para um lançamento da fatura. Fica laranja ("respondido,
    aguardando aprovação") até o financeiro aprovar (verde) ou recusar (volta a pendente, com o motivo)."""

    link = models.ForeignKey(LinkPendencias, on_delete=models.PROTECT, related_name="respostas")
    chave = models.CharField(max_length=400, db_index=True)   # chave_lancamento
    categoria = models.CharField(max_length=60, blank=True)
    descricao = models.TextField(blank=True)
    medico = models.CharField(max_length=200, blank=True)
    paciente = models.CharField(max_length=200, blank=True)
    cidade = models.CharField(max_length=120, blank=True)
    nao_reconhece = models.BooleanField(default=False)
    observacao = models.TextField(blank=True)
    respondido_em = models.DateTimeField(auto_now=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    aprovado_em = models.DateTimeField(null=True, blank=True)
    aprovado_por = models.CharField(max_length=150, blank=True)
    recusado_em = models.DateTimeField(null=True, blank=True)
    recusado_por = models.CharField(max_length=150, blank=True)
    motivo_recusa = models.TextField(blank=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        db_table = "conc_resposta_pendencia"
        ordering = ["-respondido_em"]
        constraints = [models.UniqueConstraint(fields=["chave"], condition=Q(ativo=True),
                                               name="conc_resposta_uma_ativa")]

    @property
    def situacao(self):
        if self.aprovado_em:
            return "aprovado"
        if self.recusado_em:
            return "recusado"
        return "aguardando aprovação"


class ControleCompra(models.Model):
    """Uma linha da planilha CONTROLE DE COMPRAS CARTÃO: liga o lançamento da fatura (valor na fatura, data,
    parcelas) ao número da NF e ao produto. O cartão escrito na planilha é ignorado: vale o da fatura."""

    importacao = models.ForeignKey(Importacao, on_delete=models.PROTECT, related_name="compras_controle")
    linha = models.PositiveIntegerField()
    nf_texto = models.CharField(max_length=60, blank=True, help_text="Coluna NF como veio (número, CANCELADA, SEM NF…).")
    nf_numero = models.CharField(max_length=20, blank=True)
    cancelada = models.BooleanField(default=False)
    data = models.DateField()
    produto = models.CharField(max_length=300, blank=True)
    quantidade = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    total_compra = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    valor_fatura = models.DecimalField(max_digits=12, decimal_places=2)
    parcelas = models.PositiveSmallIntegerField(default=1)
    setor = models.CharField(max_length=80, blank=True)
    oc = models.BooleanField(default=False)
    observacao = models.CharField(max_length=300, blank=True)

    class Meta:
        db_table = "conc_controle_compra"
        ordering = ["importacao", "linha"]
        verbose_name = "compra do controle"
        verbose_name_plural = "controle de compras"
        indexes = [models.Index(fields=["valor_fatura", "data"], name="conc_controle_valor_data")]

    def __str__(self):
        return f"{self.data:%d/%m/%Y} {self.produto[:40]} {self.valor_fatura}"


class GastoBase(models.Model):
    """Uma linha da base geral de despesas (planilha GERAL REPRESENTANTES): histórico anterior ao sistema e
    períodos que ainda não têm planilha enviada. Onde houver planilha do período no sistema, ela prevalece."""

    importacao = models.ForeignKey(Importacao, on_delete=models.PROTECT, related_name="gastos_base")
    linha = models.PositiveIntegerField()
    representante = models.ForeignKey(Representante, on_delete=models.PROTECT, null=True, blank=True,
                                      related_name="gastos_base")
    nome_origem = models.CharField(max_length=120)
    data = models.DateField()
    despesa_original = models.CharField(max_length=120, blank=True)
    categoria = models.CharField(max_length=40)
    descricao = models.TextField(blank=True)
    finalidade = models.CharField(max_length=120, blank=True)
    pagamento = models.CharField(max_length=60, blank=True)
    medico = models.CharField(max_length=200, blank=True)
    paciente = models.CharField(max_length=200, blank=True)
    cidade = models.CharField(max_length=120, blank=True)
    valor = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        db_table = "conc_gasto_base"
        ordering = ["importacao", "linha"]
        verbose_name = "gasto da base geral"
        verbose_name_plural = "gastos da base geral"
        indexes = [models.Index(fields=["representante", "data"], name="conc_gasto_base_rep_data")]

    def __str__(self):
        return f"{self.nome_origem} {self.data:%d/%m/%Y} {self.valor}"


class Anexo(models.Model):
    """Comprovante enviado pela tela (sem passar pela planilha). Guardado com nome = SHA-256."""

    competencia = models.ForeignKey(Competencia, on_delete=models.PROTECT, related_name="anexos")
    chave = models.CharField(max_length=400, db_index=True)
    arquivo_caminho = models.CharField(max_length=500)
    nome_original = models.CharField(max_length=255)
    mime = models.CharField(max_length=100)
    hash_sha256 = models.CharField(max_length=64)
    tamanho = models.PositiveIntegerField()
    usuario = models.CharField(max_length=150, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    ativo = models.BooleanField(default=True)
    nota = models.ForeignKey("NotaFiscal", on_delete=models.PROTECT, null=True, blank=True, related_name="anexos")
    automatico = models.BooleanField(default=False, help_text="Casado pelo sistema (nota fiscal em lote).")
    ressalva = models.CharField(max_length=200, blank=True,
                                help_text="Casamento pouco confiável (só pelo valor): o lançamento fica conciliado com ressalva.")

    class Meta:
        db_table = "conc_anexo"
        ordering = ["criado_em"]

    def __str__(self):
        return self.nome_original


class Evento(models.Model):
    """Eventos que aparecem quando a Finalidade é EVENTO. Ficam em tabela (não fixos no código): o financeiro
    cadastra novos pela própria tela. `nome_busca` (sem acento, maiúsculas, espaços únicos) bloqueia duplicado."""

    nome = models.CharField(max_length=120, unique=True)
    nome_busca = models.CharField(max_length=120, unique=True)
    # inativo: some da lista para novos lançamentos, mas continua aparecendo nos antigos (nada é apagado)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    criado_por = models.CharField(max_length=150, blank=True)

    class Meta:
        db_table = "conc_evento"
        ordering = ["nome"]

    def __str__(self):
        return self.nome


class FinalidadeLancamento(models.Model):
    """Finalidade (e evento) de um gasto, preenchida pelo financeiro ao lado de Paciente. Presa à chave natural do
    lançamento da fatura (ou do formulário, quando não há fatura), como os outros ajustes: sobrevive à reimportação.
    Nada é apagado: a troca desativa a anterior (ativo=False) e grava quem e quando."""

    class Finalidade(models.TextChoices):
        PROCEDIMENTO = "PROCEDIMENTO", "PROCEDIMENTO"
        NEGOCIACAO = "NEGOCIACAO", "NEGOCIAÇÃO"
        EVENTO = "EVENTO", "EVENTO"

    competencia = models.ForeignKey(Competencia, on_delete=models.PROTECT, related_name="finalidades")
    chave = models.CharField(max_length=400)
    finalidade = models.CharField(max_length=20, choices=Finalidade.choices, blank=True)
    evento = models.ForeignKey(Evento, on_delete=models.PROTECT, null=True, blank=True, related_name="lancamentos")
    usuario = models.CharField(max_length=150, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        db_table = "conc_finalidade_lancamento"
        ordering = ["-criado_em"]
        constraints = [
            models.UniqueConstraint(fields=["chave"], condition=models.Q(ativo=True), name="uniq_finalidade_ativa"),
            # Evento só (e obrigatório) quando a finalidade é EVENTO
            models.CheckConstraint(
                condition=(models.Q(finalidade="EVENTO", evento__isnull=False)
                           | (~models.Q(finalidade="EVENTO") & models.Q(evento__isnull=True))),
                name="evento_so_com_finalidade_evento"),
        ]

    def __str__(self):
        return f"{self.get_finalidade_display() or '—'} {self.evento or ''}".strip()


# Nome da situação na tela. A fatura é a fonte da verdade: lançamento sem formulário é "Sem justificativa" (entra
# no total, pois foi faturado) e formulário sem lançamento é "Não encontrado na fatura" (não entra). Só o texto
# muda; o código gravado no banco continua o mesmo (sem migration).
ROTULOS_TELA = {
    Resultado.Status.SEM_FORMULARIO: "Sem justificativa",
    Resultado.Status.SEM_COBRANCA: "Não encontrado na fatura",
}


def rotulo_situacao(status) -> str:
    if not status:
        return ""
    return ROTULOS_TELA.get(status) or Resultado.Status(status).label
