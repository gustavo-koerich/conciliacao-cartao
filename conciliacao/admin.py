"""Admin: cadastros (representantes, cartões) e parâmetros editáveis; o resto é só consulta.
Nada é apagado pelo admin: representante sai com "ativo" desmarcado."""
import json

from django import forms
from django.contrib import admin

from . import auditoria, parametros
from .models import (Auditoria, Backup, Cartao, Competencia, ContaCartao, Execucao, Importacao, Parametro,
                     Representante)

admin.site.site_header = "Conciliação de cartão — cadastros"
admin.site.site_title = "Conciliação"
admin.site.index_title = "Cadastros e parâmetros"


class SemApagar:
    def has_delete_permission(self, request, obj=None):
        return False


class SoConsulta(SemApagar):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class AuditaAlteracao:
    """Toda alteração de cadastro ou parâmetro vai para conc_auditoria."""

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        auditoria.registrar(
            request, "CADASTRO", "OK",
            representante=obj if isinstance(obj, Representante) else None,
            cartao=obj if isinstance(obj, Cartao) else None,
            detalhe={"modelo": obj._meta.model_name, "id": obj.pk, "alterado": change,
                     "campos": sorted(form.changed_data)},
        )


class CartaoInline(admin.TabularInline):
    model = Cartao
    fields = ("conta", "final", "nome_fatura", "escopo", "pendente_cadastro")
    extra = 0
    can_delete = False
    show_change_link = True


@admin.register(ContaCartao)
class ContaCartaoAdmin(AuditaAlteracao, SemApagar, admin.ModelAdmin):
    list_display = ("nome", "banco", "slug", "leitor", "ativo")
    list_filter = ("ativo", "leitor")
    prepopulated_fields = {"slug": ("banco",)}


@admin.register(Representante)
class RepresentanteAdmin(AuditaAlteracao, SemApagar, admin.ModelAdmin):
    list_display = ("nome", "email", "tem_planilha", "cartoes_resumo", "ativo")
    list_filter = ("ativo",)
    search_fields = ("nome", "email")
    inlines = [CartaoInline]

    @admin.display(boolean=True, description="planilha")
    def tem_planilha(self, obj):
        return bool(obj.sheet_id)

    @admin.display(description="cartões")
    def cartoes_resumo(self, obj):
        return ", ".join(f"{c.final} ({c.conta})" for c in obj.cartoes.all())

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("cartoes__conta")


@admin.register(Cartao)
class CartaoAdmin(AuditaAlteracao, SemApagar, admin.ModelAdmin):
    list_display = ("final", "conta", "nome_fatura", "representante", "escopo", "pendente_cadastro")
    list_filter = ("conta", "escopo", "pendente_cadastro")
    search_fields = ("final", "nome_fatura", "representante__nome")
    list_select_related = ("representante",)

    def save_model(self, request, obj, form, change):
        # Completar o representante tira o cartão da lista de pendentes de cadastro.
        if obj.representante_id or obj.escopo == Cartao.Escopo.FORA_ESCOPO:
            obj.pendente_cadastro = False
        super().save_model(request, obj, form, change)


class ParametroForm(forms.ModelForm):
    valor = forms.CharField(widget=forms.Textarea(attrs={"rows": 14, "style": "font-family:monospace;width:100%"}),
                            help_text="JSON. Texto entre aspas, números sem aspas; decimais como texto (\"0.05\").")

    class Meta:
        model = Parametro
        fields = ("valor",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["valor"] = json.dumps(self.instance.valor, ensure_ascii=False, indent=2)

    def clean_valor(self):
        try:
            bruto = json.loads(self.cleaned_data["valor"])
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"JSON inválido: {exc.msg} (linha {exc.lineno}).") from exc
        try:
            return parametros.validar(self.instance.chave, bruto)
        except parametros.ParametroInvalido as exc:
            raise forms.ValidationError(str(exc)) from exc


@admin.register(Parametro)
class ParametroAdmin(AuditaAlteracao, SemApagar, admin.ModelAdmin):
    form = ParametroForm
    list_display = ("chave", "valor_resumido", "atualizado_em", "atualizado_por")
    readonly_fields = ("chave", "descricao", "atualizado_em", "atualizado_por")
    fields = ("chave", "descricao", "valor", "atualizado_em", "atualizado_por")

    def has_add_permission(self, request):
        return False  # o catálogo de parâmetros é definido no código (parametros.py)

    @admin.display(description="valor")
    def valor_resumido(self, obj):
        texto = json.dumps(obj.valor, ensure_ascii=False)
        return texto if len(texto) <= 80 else texto[:77] + "…"

    def save_model(self, request, obj, form, change):
        acesso = getattr(request, "usuario_acesso", None) or {}
        obj.atualizado_por = acesso.get("usuario") or request.user.get_username()
        super().save_model(request, obj, form, change)


@admin.register(Competencia)
class CompetenciaAdmin(SoConsulta, admin.ModelAdmin):
    list_display = ("ano_mes", "conta", "periodo_ini", "periodo_fim", "vencimento", "total_fatura", "status")
    list_filter = ("conta", "status")


@admin.register(Importacao)
class ImportacaoAdmin(SoConsulta, admin.ModelAdmin):
    list_display = ("criado_em", "tipo", "competencia", "representante", "versao", "origem", "metodo_leitura",
                    "status", "ativo", "usuario", "ip")
    list_filter = ("tipo", "status", "ativo", "origem")
    list_select_related = ("competencia", "representante")


@admin.register(Execucao)
class ExecucaoAdmin(SoConsulta, admin.ModelAdmin):
    list_display = ("inicio", "competencia", "escopo", "motivo", "status", "fim", "usuario")
    list_filter = ("status",)


@admin.register(Auditoria)
class AuditoriaAdmin(SoConsulta, admin.ModelAdmin):
    list_display = ("momento", "acao", "resultado", "usuario", "ip", "maquina", "competencia", "cartao")
    list_filter = ("acao", "resultado")
    search_fields = ("usuario", "ip", "maquina")


@admin.register(Backup)
class BackupAdmin(SoConsulta, admin.ModelAdmin):
    list_display = ("momento", "tipo", "status", "arquivo", "tamanho", "destino", "duracao_seg")
    list_filter = ("tipo", "status")
