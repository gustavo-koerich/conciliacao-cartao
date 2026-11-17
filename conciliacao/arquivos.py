"""Guarda arquivos enviados fora do diretório estático, com nome = SHA-256 do conteúdo."""
import hashlib
import shutil
from pathlib import Path

from django.conf import settings
from django.utils import timezone


class ArquivoRecusado(Exception):
    pass


def _em_pedacos(caminho, tamanho=1024 * 1024):
    with open(caminho, "rb") as f:
        while pedaco := f.read(tamanho):
            yield pedaco


def guardar(origem, nome_original: str, *, subpasta: str, extensoes: set, assinatura: bytes,
            descricao: str) -> tuple[str, Path]:
    """Confere extensão, assinatura (bytes iniciais) e tamanho; grava em ARQUIVOS_DIR/<subpasta>/<sha256><ext>.
    origem: arquivo enviado (UploadedFile) ou caminho no disco. Devolve (hash, caminho)."""
    extensao = Path(nome_original).suffix.lower()
    if extensao not in extensoes:
        raise ArquivoRecusado(f"Envie {descricao}.")
    limite = settings.UPLOAD_MAX_MB * 1024 * 1024
    destino_dir = Path(settings.ARQUIVOS_DIR) / subpasta
    destino_dir.mkdir(parents=True, exist_ok=True)
    temporario = destino_dir / f".recebendo-{timezone.now():%Y%m%d%H%M%S%f}"
    sha = hashlib.sha256()
    tamanho = 0
    try:
        with open(temporario, "wb") as saida:
            pedacos = origem.chunks() if hasattr(origem, "chunks") else _em_pedacos(origem)
            for i, pedaco in enumerate(pedacos):
                if i == 0 and not pedaco.startswith(assinatura):
                    raise ArquivoRecusado(f"O arquivo não é {descricao} válido.")
                tamanho += len(pedaco)
                if tamanho > limite:
                    raise ArquivoRecusado(f"Arquivo maior que {settings.UPLOAD_MAX_MB} MB.")
                sha.update(pedaco)
                saida.write(pedaco)
        if tamanho == 0:
            raise ArquivoRecusado("Arquivo vazio.")
        final = destino_dir / f"{sha.hexdigest()}{extensao}"
        if final.exists():
            temporario.unlink()
        else:
            shutil.move(temporario, final)
        return sha.hexdigest(), final
    finally:
        if temporario.exists():
            temporario.unlink()
