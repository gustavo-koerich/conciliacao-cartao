"""Finalidade e Evento de um gasto (preenchidos pelo financeiro ao lado de Paciente).

Evento: tabela própria (conc_evento). Nome normalizado (maiúsculas, espaços únicos, prefixo "EVENTO ") e duplicado
bloqueado comparando sem acento e sem diferença de maiúsculas. Evento só existe com Finalidade = EVENTO (regra
também garantida no banco). Nada é apagado: a troca desativa a escolha anterior e grava quem e quando."""
import unicodedata

from django.db import IntegrityError, transaction

from . import auditoria
from .models import Evento, FinalidadeLancamento

F = FinalidadeLancamento.Finalidade


class EscolhaInvalida(ValueError):
    """Mensagem já pronta para mostrar na tela."""


def normalizar_nome(nome: str) -> str:
    """ " evento  aorta " -> "EVENTO AORTA"; "Carioca" -> "EVENTO CARIOCA"."""
    texto = " ".join((nome or "").upper().split())
    if texto and texto != "EVENTO" and not texto.startswith("EVENTO "):
        texto = f"EVENTO {texto}"
    return texto


def chave_busca(nome: str) -> str:
    """Igual à da migration 0019: sem acento, maiúsculas, espaços únicos (bloqueia "Milão" x "MILAO")."""
    sem_acento = "".join(c for c in unicodedata.normalize("NFKD", nome) if not unicodedata.combining(c))
    return " ".join(sem_acento.upper().split())


def ativos():
    return Evento.objects.filter(ativo=True).order_by("nome")


def cadastrar(nome: str, *, usuario="") -> Evento:
    nome = normalizar_nome(nome)
    if len(nome) <= len("EVENTO ") or len(nome) > 120:
        raise EscolhaInvalida("Digite o nome do evento (até 120 letras).")
    busca = chave_busca(nome)
    existente = Evento.objects.filter(nome_busca=busca).first()
    if existente is not None:
        raise EscolhaInvalida(f"Já existe o evento “{existente.nome}”. Escolha ele na lista.")
    try:
        with transaction.atomic():
            evento = Evento.objects.create(nome=nome, nome_busca=busca, criado_por=usuario)
    except IntegrityError as exc:   # dois cadastros ao mesmo tempo
        raise EscolhaInvalida("Esse evento acabou de ser cadastrado. Escolha ele na lista.") from exc
    auditoria.registrar(None, "CADASTRO", "OK", usuario=usuario, detalhe={"evento_novo": nome})
    return evento


def atual(chave: str):
    return FinalidadeLancamento.objects.filter(chave=chave, ativo=True).select_related("evento").first()


def escolher(resultado, chave: str, finalidade: str, evento_id: str = "", novo_evento: str = "", *,
             usuario="", ip=None):
    """Grava a finalidade (e o evento) da linha. Finalidade vazia limpa a escolha. Se a finalidade não for EVENTO,
    o evento é limpo. Devolve a escolha nova (ou None)."""
    finalidade = (finalidade or "").strip()
    if finalidade and finalidade not in F.values:
        raise EscolhaInvalida("Finalidade inválida.")
    evento = None
    if finalidade == F.EVENTO:
        if evento_id == "novo":
            evento = cadastrar(novo_evento, usuario=usuario)
        elif evento_id:
            evento = Evento.objects.filter(pk=evento_id).first() if str(evento_id).isdigit() else None
            if evento is None:
                raise EscolhaInvalida("Evento não encontrado.")
            if not evento.ativo:
                raise EscolhaInvalida(f"O evento “{evento.nome}” está inativo.")
        else:
            raise EscolhaInvalida("Com a finalidade EVENTO, escolha o evento (ou cadastre um novo).")
    anterior = atual(chave)
    if anterior and anterior.finalidade == finalidade and anterior.evento_id == (evento.pk if evento else None):
        return anterior
    with transaction.atomic():
        FinalidadeLancamento.objects.filter(chave=chave, ativo=True).update(ativo=False)
        nova = None
        if finalidade:
            nova = FinalidadeLancamento.objects.create(competencia=resultado.competencia, chave=chave,
                                                       finalidade=finalidade, evento=evento, usuario=usuario, ip=ip)
    auditoria.registrar(None, "CADASTRO", "OK", competencia=resultado.competencia,
                        representante=resultado.representante, usuario=usuario, ip=ip,
                        detalhe={"finalidade": finalidade or "(limpa)", "evento": evento.nome if evento else "",
                                 "antes": str(anterior) if anterior else "", "resultado": resultado.pk})
    return nova
