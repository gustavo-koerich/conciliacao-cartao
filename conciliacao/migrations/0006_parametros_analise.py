"""Parâmetros da análise de gastos (categorias e de-para de representantes). Não sobrescreve valores editados."""
from django.db import migrations

PADROES = {
    "categorias_analise": (
        {"alimentacao": "Alimentação", "alimentacao com medico": "Alimentação com médico",
         "combustivel": "Combustível", "estacionamento": "Estacionamento",
         "estacionamento aeroporto": "Estacionamento", "brindes/presentes": "Brindes e presentes",
         "brindes e presentes": "Brindes e presentes", "manutencao veicular": "Manutenção veicular"},
        "Dashboard e lista geral: tipo de gasto como veio (minúsculas, sem acento) → categoria da análise. "
        "O que não estiver aqui vira \"Outros\".",
    ),
    "de_para_representantes": (
        {"jose valente": "José", "jose henrique valente": "José", "jose": "José", "fabio": "Fábio",
         "otavio": "Otávio"},
        "Base geral: nome como aparece na planilha (minúsculas, sem acento) → nome do cadastro de representantes. "
        "Sem correspondência, o sistema procura pelo nome igual no cadastro.",
    ),
}


def criar(apps, schema_editor):
    Parametro = apps.get_model("conciliacao", "Parametro")
    for chave, (valor, descricao) in PADROES.items():
        Parametro.objects.get_or_create(chave=chave, defaults={"valor": valor, "descricao": descricao,
                                                               "atualizado_por": "migration"})


class Migration(migrations.Migration):
    dependencies = [("conciliacao", "0005_base_geral")]
    operations = [migrations.RunPython(criar, migrations.RunPython.noop)]
