"""Catálogo dos parâmetros do motor: tipo, padrão e descrição.

Os valores ficam em conc_parametro (editáveis no admin). Cada execução grava uma cópia em
conc_execucao.parametros, para o resultado ser reproduzível.
Valores monetários e percentuais são guardados como texto ("0.05") para não passar por float.
"""
import unicodedata
from decimal import Decimal, InvalidOperation

INTEIRO = "inteiro"
DECIMAL = "decimal"
DE_PARA = "de_para"            # {"texto normalizado": "valor canônico"}
LISTAS = "listas"              # {"categoria": ["palavra", ...]}

CATALOGO = {
    "janela_dias": {
        "tipo": INTEIRO, "padrao": 3,
        "descricao": "Dias de distância entre a data da fatura e a do formulário. Acima disso o casamento vira ressalva "
                     "(valor exato é sempre obrigatório; a data só desempata).",
    },
    "tolerancia_parcela_reais": {
        "tipo": DECIMAL, "padrao": "0.05",
        "descricao": "Regra 4 (parcelado): diferença aceita por parcela entre parcela × N e o total do formulário, "
                     "para cobrir o arredondamento do banco.",
    },
    "ciclos_pendente_analise": {
        "tipo": INTEIRO, "padrao": 2,
        "descricao": "Competências abertas seguintes que uma despesa SEM_COBRANCA espera antes de virar PENDENTE_ANALISE.",
    },
    "max_itens_conta_dividida": {
        "tipo": INTEIRO, "padrao": 3,
        "descricao": "Regra 6: quantidade máxima de itens somados de um lado para casar com um item do outro.",
    },
    "ano_minimo_data": {
        "tipo": INTEIRO, "padrao": 2024,
        "descricao": "Data do formulário com ano menor que este recebe a marca DATA_SUSPEITA.",
    },
    "de_para_medicos": {
        "tipo": DE_PARA,
        "padrao": {
            "sem medico": "SEM MÉDICO",
            "sem medicos": "SEM MÉDICO",
            "sem medio": "SEM MÉDICO",
            "sem medico vinculado": "SEM MÉDICO",
            "sem": "SEM MÉDICO",
            "varios": "VÁRIOS",
            "varios medicos": "VÁRIOS",
            "varios vasculares": "VÁRIOS",
        },
        "descricao": "Canonização do campo Médico. A chave é o texto em minúsculas, sem acentos e sem espaços extras.",
    },
    "palavras_categoria": {
        "tipo": LISTAS,
        "padrao": {
            "Alimentação": ["restaurante", "lanch", "padaria", "cafe", "pizz", "burger", "bar ", "churrasc",
                            "food", "sushi", "giraffas", "mcdonald", "subway", "ifood", "bistro", "cucina"],
            "Estacionamento": ["estacion", "park", "parking", "estapar", "zona azul", "garag"],
            "Combustível": ["posto", "combust", "petro", "shell", "ipiranga", "br mania", "auto posto"],
            "Hospedagem": ["hotel", "pousada", "airbnb", "booking", "hostel", "inn ", "resort", "flat"],
            "Uber/Táxi": ["uber", "99 ", "99app", "taxi", "cabify"],
            "Pedágio": ["pedagio", "sem parar", "conectcar", "veloe", "move mais"],
        },
        "descricao": "Palavras do estabelecimento (fatura) que indicam cada categoria do formulário. Se o estabelecimento "
                     "bate com outra categoria e não com a informada, o casamento vira ressalva (não bloqueia).",
    },
    "categorias_analise": {
        "tipo": DE_PARA,
        "padrao": {
            "alimentacao": "Alimentação",
            "alimentacao com medico": "Alimentação com médico",
            "combustivel": "Combustível",
            "estacionamento": "Estacionamento",
            "estacionamento aeroporto": "Estacionamento",
            "brindes/presentes": "Brindes e presentes",
            "brindes e presentes": "Brindes e presentes",
            "manutencao veicular": "Manutenção veicular",
        },
        "descricao": "Dashboard e lista geral: tipo de gasto como veio (minúsculas, sem acento) → categoria da análise. "
                     "O que não estiver aqui vira \"Outros\".",
    },
    "de_para_representantes": {
        "tipo": DE_PARA,
        "padrao": {"jose valente": "José", "jose henrique valente": "José", "jose": "José", "fabio": "Fábio",
                   "otavio": "Otávio"},
        "descricao": "Base geral: nome como aparece na planilha (minúsculas, sem acento) → nome do cadastro de "
                     "representantes. Sem correspondência, o sistema procura pelo nome igual no cadastro.",
    },
}

CATEGORIAS_ANALISE = ["Alimentação", "Combustível", "Estacionamento", "Alimentação com médico", "Brindes e presentes",
                      "Manutenção veicular", "Outros"]

# Lista suspensa "No formulário" na tela do representante (pedido de 22/10/2026) e a categoria da análise de cada uma.
CATEGORIAS_DESPESA = {
    "Alimentação pessoal": "Alimentação",
    "Alimentação com médicos": "Alimentação com médico",
    "Brindes/presentes": "Brindes e presentes",
    "Manutenção veicular": "Manutenção veicular",
    "Combustível": "Combustível",
    "Frete": "Outros",
    "Despesa com eventos": "Outros",
    "Estacionamento": "Estacionamento",
    "Estacionamento aeroporto": "Estacionamento",
    "Locação veicular": "Outros",
    "Hospedagem": "Outros",
    "Conservação predial": "Outros",
    "Correios": "Outros",
    "Uber/táxi": "Outros",
    "Material de expediente": "Outros",
    "Multa de trânsito": "Outros",
    "Pedágio": "Outros",
    "Manutenção de Malas": "Outros",
    "Outros": "Outros",
}


def categoria_da_lista(despesa: str) -> str:
    """A opção da lista que é exatamente o que foi informado (sem acento/maiúsculas), ou "" se não houver."""
    alvo = normalizar_texto(despesa)
    return next((c for c in CATEGORIAS_DESPESA if normalizar_texto(c) == alvo), "")


class ParametroInvalido(ValueError):
    pass


def normalizar_texto(texto: str) -> str:
    """Minúsculas, sem acentos, espaços simples. Usado nas chaves do de-para."""
    sem_acento = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return " ".join(sem_acento.lower().split())


def validar(chave: str, valor):
    """Confere o valor contra o catálogo e devolve a forma normalizada para gravar."""
    if chave not in CATALOGO:
        raise ParametroInvalido(f"Parâmetro desconhecido: {chave}")
    tipo = CATALOGO[chave]["tipo"]
    if tipo == INTEIRO:
        if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
            raise ParametroInvalido("Use um número inteiro maior ou igual a zero.")
        return valor
    if tipo == DECIMAL:
        try:
            numero = Decimal(str(valor))
        except InvalidOperation as exc:
            raise ParametroInvalido("Use um número decimal com ponto, entre aspas. Ex.: \"0.05\".") from exc
        if not numero.is_finite() or numero < 0:
            raise ParametroInvalido("Use um número decimal maior ou igual a zero.")
        return str(numero)
    if tipo == DE_PARA:
        if not isinstance(valor, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in valor.items()):
            raise ParametroInvalido('Use um objeto {"texto": "valor canônico"}.')
        limpo = {normalizar_texto(k): v.strip() for k, v in valor.items() if normalizar_texto(k)}
        if chave == "categorias_analise":
            invalidas = sorted({v for v in limpo.values() if v not in CATEGORIAS_ANALISE})
            if invalidas:
                raise ParametroInvalido(f"Categoria fora da lista ({', '.join(CATEGORIAS_ANALISE)}): "
                                        f"{', '.join(invalidas)}")
        return limpo
    if tipo == LISTAS:
        if not isinstance(valor, dict) or not all(
                isinstance(k, str) and isinstance(v, list) and all(isinstance(p, str) for p in v)
                for k, v in valor.items()):
            raise ParametroInvalido('Use um objeto {"Categoria": ["palavra", ...]}.')
        return {k.strip(): [p.lower() for p in v if p.strip()] for k, v in valor.items()}
    raise ParametroInvalido(f"Tipo sem validação: {tipo}")


def obter_todos() -> dict:
    """Snapshot de todos os parâmetros. Falta de parâmetro no banco é erro: a execução não roda com padrão escondido."""
    from .models import Parametro

    valores = dict(Parametro.objects.values_list("chave", "valor"))
    faltando = sorted(set(CATALOGO) - set(valores))
    if faltando:
        raise ParametroInvalido(f"Parâmetros ausentes no banco: {', '.join(faltando)}")
    return {chave: validar(chave, valores[chave]) for chave in CATALOGO}
