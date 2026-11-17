from django.conf import settings


def acesso(request):
    return {
        "usuario_acesso": getattr(request, "usuario_acesso", None),
        "ip_cliente": getattr(request, "ip_cliente", ""),
        "app_versao": settings.APP_VERSAO,
        # Pela Central: "Sair" encerra a sessão da Central (quem só usa a Conciliação não tem outro menu).
        "sair_url": "/logout" if settings.ACESSO_MODO == "central" else "",
    }
