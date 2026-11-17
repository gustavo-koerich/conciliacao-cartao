"""Grava os parâmetros padrão do catálogo. Não sobrescreve valores já editados no admin."""
from django.db import migrations

# Cópia fixa do catálogo na data desta migration: a migration não pode mudar se parametros.py mudar depois.
PADROES = {
    "janela_dias": (3, "Dias de distância entre a data da fatura e a do formulário. Acima disso o casamento vira "
                       "ressalva (valor exato é sempre obrigatório; a data só desempata)."),
    "tolerancia_parcela_reais": ("0.05", "Regra 4 (parcelado): diferença aceita por parcela entre parcela × N e o "
                                         "total do formulário, para cobrir o arredondamento do banco."),
    "ciclos_pendente_analise": (2, "Competências abertas seguintes que uma despesa SEM_COBRANCA espera antes de virar "
                                   "PENDENTE_ANALISE."),
    "max_itens_conta_dividida": (3, "Regra 6: quantidade máxima de itens somados de um lado para casar com um item "
                                    "do outro."),
    "ano_minimo_data": (2024, "Data do formulário com ano menor que este recebe a marca DATA_SUSPEITA."),
    "de_para_medicos": (
        {
            "sem medico": "SEM MÉDICO", "sem medicos": "SEM MÉDICO", "sem medio": "SEM MÉDICO",
            "sem medico vinculado": "SEM MÉDICO", "sem": "SEM MÉDICO",
            "varios": "VÁRIOS", "varios medicos": "VÁRIOS", "varios vasculares": "VÁRIOS",
        },
        "Canonização do campo Médico. A chave é o texto em minúsculas, sem acentos e sem espaços extras.",
    ),
    "palavras_categoria": (
        {
            "Alimentação": ["restaurante", "lanch", "padaria", "cafe", "pizz", "burger", "bar ", "churrasc",
                            "food", "sushi", "giraffas", "mcdonald", "subway", "ifood", "bistro", "cucina"],
            "Estacionamento": ["estacion", "park", "parking", "estapar", "zona azul", "garag"],
            "Combustível": ["posto", "combust", "petro", "shell", "ipiranga", "br mania", "auto posto"],
            "Hospedagem": ["hotel", "pousada", "airbnb", "booking", "hostel", "inn ", "resort", "flat"],
            "Uber/Táxi": ["uber", "99 ", "99app", "taxi", "cabify"],
            "Pedágio": ["pedagio", "sem parar", "conectcar", "veloe", "move mais"],
        },
        "Palavras do estabelecimento (fatura) que indicam cada categoria do formulário. Se o estabelecimento bate com "
        "outra categoria e não com a informada, o casamento vira ressalva (não bloqueia).",
    ),
}


def criar(apps, schema_editor):
    Parametro = apps.get_model("conciliacao", "Parametro")
    for chave, (valor, descricao) in PADROES.items():
        Parametro.objects.get_or_create(chave=chave, defaults={"valor": valor, "descricao": descricao,
                                                               "atualizado_por": "migration"})


class Migration(migrations.Migration):
    dependencies = [("conciliacao", "0001_initial")]
    operations = [migrations.RunPython(criar, migrations.RunPython.noop)]
