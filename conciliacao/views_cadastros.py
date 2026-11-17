"""Cadastros pela tela: bancos (contas de cartão), cartões e representantes. Tudo auditado; nada é apagado."""
from django import forms
from django.contrib import messages
from django.db.models import Count, Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from . import auditoria, motor
from .models import Cartao, Competencia, ContaCartao, Representante


class ContaForm(forms.ModelForm):
    class Meta:
        model = ContaCartao
        fields = ["nome", "banco", "leitor", "ativo"]
        labels = {"nome": "Nome da conta", "banco": "Nome curto", "leitor": "Como ler a fatura"}

    def save(self, commit=True):
        conta = super().save(commit=False)
        if not conta.slug:
            base = slugify(conta.banco)[:24] or "banco"
            slug, n = base, 2
            while ContaCartao.objects.filter(slug=slug).exclude(pk=conta.pk).exists():
                slug, n = f"{base}-{n}", n + 1
            conta.slug = slug
        if commit:
            conta.save()
        return conta


class CartaoForm(forms.ModelForm):
    class Meta:
        model = Cartao
        fields = ["nome_fatura", "representante", "escopo"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["representante"].queryset = Representante.objects.filter(ativo=True).order_by("nome")
        self.fields["representante"].required = False


class RepresentanteForm(forms.ModelForm):
    class Meta:
        model = Representante
        fields = ["nome", "email", "ativo"]


def cadastros(request):
    contas = (ContaCartao.objects.annotate(n_competencias=Count("competencias", distinct=True))
              .prefetch_related(Prefetch("cartoes", queryset=Cartao.objects.select_related("representante")
                                         .order_by("final"))))
    reps = Representante.objects.annotate(n_cartoes=Count("cartoes")).order_by("-ativo", "nome")
    return render(request, "conciliacao/cadastros.html", {
        "contas": contas, "representantes": reps, "nova_conta": ContaForm(prefix="nova"),
        "novo_rep": RepresentanteForm(prefix="novo", initial={"ativo": True}),
        "leitores": ContaCartao.Leitor.choices, "escopos": Cartao.Escopo.choices,
        "reps_ativos": Representante.objects.filter(ativo=True).order_by("nome"),
        "pendentes": Cartao.objects.filter(pendente_cadastro=True).count()})


def _erros(form):
    return "; ".join(f"{form.fields[c].label if c in form.fields else c}: {' '.join(e)}" for c, e in form.errors.items())


def _usuario(request):
    return (getattr(request, "usuario_acesso", None) or {}).get("usuario", "")


@require_POST
def salvar_conta(request, pk=None):
    conta = get_object_or_404(ContaCartao, pk=pk) if pk else None
    form = ContaForm(request.POST, instance=conta, prefix=None if pk else "nova")
    if not form.is_valid():
        messages.error(request, f"Banco não salvo. {_erros(form)}")
        return redirect("conciliacao:cadastros")
    conta = form.save()
    auditoria.registrar(request, "CADASTRO", "OK", detalhe={"modelo": "conta_cartao", "id": conta.pk,
                                                            "novo": pk is None, "campos": sorted(form.changed_data)})
    situacao = "Ele já aparece na importação de fatura." if conta.ativo else "Inativo: não aparece na importação."
    messages.success(request, f"Banco {conta.nome} {'criado' if pk is None else 'atualizado'}. {situacao}")
    return redirect("conciliacao:cadastros")


@require_POST
def salvar_cartao(request, pk):
    cartao = get_object_or_404(Cartao.objects.select_related("conta"), pk=pk)
    form = CartaoForm(request.POST, instance=cartao)
    if not form.is_valid():
        messages.error(request, f"Cartão {cartao.final} não salvo. {_erros(form)}")
        return redirect("conciliacao:cadastros")
    cartao = form.save(commit=False)
    if cartao.representante_id or cartao.escopo == Cartao.Escopo.FORA_ESCOPO:
        cartao.pendente_cadastro = False
    cartao.save()
    auditoria.registrar(request, "CADASTRO", "OK", cartao=cartao,
                        detalhe={"modelo": "cartao", "id": cartao.pk, "campos": sorted(form.changed_data)})
    # Mudou representante ou escopo: refaz a conciliação desse cartão nas competências abertas do banco.
    if {"representante", "escopo"} & set(form.changed_data):
        for comp in Competencia.objects.filter(conta=cartao.conta, status=Competencia.Status.ABERTA,
                                               importacoes__tipo="FATURA", importacoes__ativo=True).distinct():
            motor.executar(comp, [cartao], motivo="cadastro do cartão", usuario=_usuario(request))
    messages.success(request, f"Cartão {cartao.final} ({cartao.conta}) salvo.")
    return redirect("conciliacao:cadastros")


@require_POST
def novo_cartao(request, conta_pk):
    """Cartão cadastrado à mão (antes da primeira fatura, ou quando o banco não traz o nome)."""
    conta = get_object_or_404(ContaCartao, pk=conta_pk)
    nome_novo = request.POST.get("novo_representante", "").strip()[:120]
    if nome_novo:
        rep, _ = Representante.objects.get_or_create(nome=nome_novo)
        dados = request.POST.copy()
        dados["representante"] = rep.pk
    else:
        dados = request.POST
    form = NovoCartaoForm(dados, instance=Cartao(conta=conta))
    if not form.is_valid():
        messages.error(request, f"Cartão não cadastrado. {_erros(form)}")
        return redirect("conciliacao:cadastros")
    cartao = form.save()
    auditoria.registrar(request, "CADASTRO", "OK", cartao=cartao,
                        detalhe={"modelo": "cartao", "id": cartao.pk, "novo": True, "conta": conta.slug})
    messages.success(request, f"Cartão {cartao.final} cadastrado no {conta}.")
    return redirect("conciliacao:cadastros")


class NovoCartaoForm(CartaoForm):
    class Meta(CartaoForm.Meta):
        fields = ["final", "nome_fatura", "representante", "escopo"]

    def validate_unique(self):
        super().validate_unique()
        if Cartao.objects.filter(conta=self.instance.conta, final=self.cleaned_data.get("final")).exists():
            self.add_error("final", "Este banco já tem um cartão com esse final.")


@require_POST
def salvar_representante(request, pk=None):
    rep = get_object_or_404(Representante, pk=pk) if pk else None
    form = RepresentanteForm(request.POST, instance=rep, prefix=None if pk else "novo")
    if not form.is_valid():
        messages.error(request, f"Representante não salvo. {_erros(form)}")
        return redirect("conciliacao:cadastros")
    rep = form.save()
    auditoria.registrar(request, "CADASTRO", "OK", representante=rep,
                        detalhe={"modelo": "representante", "id": rep.pk, "novo": pk is None,
                                 "campos": sorted(form.changed_data)})
    messages.success(request, f"Representante {rep.nome} salvo.")
    return redirect("conciliacao:cadastros")


def exportar_base(request):
    """Base inteira em .zip (CSV por tabela + dicionário). Nome de paciente sai mascarado."""
    import io

    from django.http import HttpResponse
    from django.utils import timezone

    from . import exportacao
    buf = io.BytesIO()
    contagem = exportacao.exportar(buf)
    auditoria.registrar(request, "EXPORTACAO", "OK", detalhe={"linhas": sum(contagem.values()), "completo": False})
    resp = HttpResponse(buf.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="conciliacao-{timezone.localtime():%Y%m%d-%H%M}.zip"'
    return resp
