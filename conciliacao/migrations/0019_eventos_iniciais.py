"""Carga inicial da tabela nova de eventos (conc_evento). Só insere na tabela criada pela 0018; não toca em nenhum
dado existente. Desfazer apaga apenas estes 16 nomes, e só se nenhum lançamento os usar."""
import unicodedata

from django.db import migrations

EVENTOS = [
    "EVENTO AORTA", "EVENTO CARIOCA", "EVENTO CICE", "EVENTO CISP", "EVENTO CIVAT", "EVENTO SBACV - BA",
    "EVENTO SBACV - RJ", "EVENTO INOVASC", "EVENTO PERNAMBUCANO", "EVENTO ENCONTRO MINEIRO",
    "EVENTO ENCONTRO NORTE E NORDESTE", "EVENTO MILÃO", "EVENTO REUNIÃO CIENTÍFICA", "EVENTO SCIENCE",
    "EVENTO SEIVASC", "EVENTO VEITH - NY",
]


def _busca(nome):
    # mesma regra do cadastro pela tela: sem acento, maiúsculas, espaços únicos
    sem_acento = "".join(c for c in unicodedata.normalize("NFKD", nome) if not unicodedata.combining(c))
    return " ".join(sem_acento.upper().split())


def criar(apps, schema_editor):
    Evento = apps.get_model("conciliacao", "Evento")
    for nome in EVENTOS:
        Evento.objects.get_or_create(nome_busca=_busca(nome), defaults={"nome": nome, "criado_por": "carga inicial"})


def desfazer(apps, schema_editor):
    Evento = apps.get_model("conciliacao", "Evento")
    Evento.objects.filter(nome__in=EVENTOS, lancamentos__isnull=True, criado_por="carga inicial").delete()


class Migration(migrations.Migration):
    dependencies = [("conciliacao", "0018_finalidade_evento")]
    operations = [migrations.RunPython(criar, desfazer)]
