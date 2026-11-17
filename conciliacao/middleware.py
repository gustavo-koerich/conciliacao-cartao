"""Controle de acesso em duas camadas.

1. RedeMiddleware: só aceita IPs das faixas em ALLOWED_NETWORKS (403 fora delas).
2. AcessoMiddleware: identifica a pessoa, conforme ACESSO_MODO:
   - central: valida o cookie da Central de Automações em /api/auth/verificar (padrão);
   - rede: sem identificação, só o filtro de rede;
   - django: login próprio do Django (para quando a aplicação sair da Central).

As views leem sempre request.usuario_acesso ({"usuario", "nome", "admin"} ou None), nunca o modo.
"""
import hashlib
import logging

import requests
from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.auth.views import redirect_to_login
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import set_script_prefix

from . import externo
from .rede import ip_do_cliente, ip_na_faixa

log = logging.getLogger(__name__)

_BACKEND_MODELO = "django.contrib.auth.backends.ModelBackend"


def _rota_livre(request) -> bool:
    return request.path_info.startswith(settings.ROTAS_LIVRES)


def _eh_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


class RedeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.portal = externo.eh_portal(request)
        if request.portal:
            return self._portal(request)
        request.externo = externo.eh_externo(request)
        if request.externo:
            # link da internet: token do Cloudflare Access em todo pedido; admin do Django só pela rede interna
            request.ip_cliente = externo.ip_externo(request)
            try:
                externo.conferir(request)
                if request.path_info.startswith("/admin/"):
                    raise externo.AcessoNegado("admin só pela rede interna")
            except externo.AcessoNegado as exc:
                log.warning("acesso externo recusado", extra={"ip": request.ip_cliente, "motivo": str(exc)})
                return HttpResponse("Acesso negado.", status=403, content_type="text/plain; charset=utf-8")
            return self.get_response(request)
        request.ip_cliente = ip_do_cliente(request)
        if not _rota_livre(request) and not ip_na_faixa(request.ip_cliente, settings.ALLOWED_NETWORKS):
            log.warning("acesso recusado: IP fora das redes permitidas",
                        extra={"ip": request.ip_cliente, "caminho": request.path_info})
            return render(request, "conciliacao/bloqueado.html",
                          {"motivo": "Este endereço de rede não tem acesso à conciliação."}, status=403)
        return self.get_response(request)

    def _portal(self, request):
        """Porta do portal de pendências (aberta na internet pelo Tailscale Funnel): só /pendencias/, sem login da
        Central e sem o filtro de redes (o representante vem da internet). O link carrega o próprio código secreto:
        nada de cache, de indexação nem de enviar o endereço para outros sites."""
        request.ip_cliente = externo.ultimo_encaminhado(request)
        if not request.path_info.startswith("/pendencias/"):
            return HttpResponse("Não encontrado.", status=404, content_type="text/plain; charset=utf-8")
        set_script_prefix("/")
        resposta = self.get_response(request)
        resposta["Cache-Control"] = "no-store"
        # same-origin: o endereço (com o código) não sai para outros sites, e o navegador ainda manda o Origin
        # que a proteção CSRF confere ("no-referrer" faria o celular mandar Origin: null e recusar o envio).
        resposta["Referrer-Policy"] = "same-origin"
        # o cookie do CSRF fica em /conciliacao (prefixo da rede interna); no portal ele precisa valer em /pendencias/
        if settings.CSRF_COOKIE_NAME in resposta.cookies:
            resposta.cookies[settings.CSRF_COOKIE_NAME]["path"] = "/pendencias/"
        resposta["X-Robots-Tag"] = "noindex, nofollow"
        return resposta


class AcessoMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.usuario_acesso = None
        if _rota_livre(request) or getattr(request, "portal", False):   # portal: o código do link é a autorização
            return self.get_response(request)
        modo = settings.ACESSO_MODO
        if modo == "central":
            resposta = self._central(request)
        elif modo == "django":
            resposta = self._django(request)
        else:
            resposta = None
        return resposta or self.get_response(request)

    # ---------- login do Django ----------

    def _django(self, request):
        if request.path_info.startswith("/admin/login"):
            return None
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        u = request.user
        request.usuario_acesso = {"usuario": u.get_username(), "nome": u.get_full_name() or u.get_username(),
                                  "admin": u.is_superuser}
        return None

    # ---------- login da Central ----------

    def _ir_para_login(self, request):
        if _eh_htmx(request):
            resposta = HttpResponse(status=204)
            resposta["HX-Redirect"] = settings.CENTRAL_LOGIN_URL
            return resposta
        return redirect(settings.CENTRAL_LOGIN_URL)

    def _consultar_central(self, cookie: str, externo_: bool = False):
        """Retorna o dict da Central, None se a sessão não vale (401). Erro de comunicação sobe como exceção.
        externo_: o pedido veio pelo link da internet — a Central aplica as regras de sessão externa."""
        chave = "central-sessao:" + ("ext:" if externo_ else "") + hashlib.sha256(cookie.encode()).hexdigest()
        guardado = cache.get(chave)
        if guardado is not None:
            return guardado
        resp = requests.get(
            settings.CENTRAL_VERIFY_URL,
            params={"app": settings.CENTRAL_APP_ID},
            headers={"X-Acesso-Externo": "1"} if externo_ else {},
            cookies={settings.CENTRAL_COOKIE_NAME: cookie},
            timeout=(2, 5),
            allow_redirects=False,
        )
        if resp.status_code == 401:
            return None
        resp.raise_for_status()
        dados = resp.json()
        if not isinstance(dados, dict) or "usuario" not in dados or "permitido" not in dados:
            raise ValueError("resposta inesperada da Central")
        cache.set(chave, dados, settings.CENTRAL_CACHE_SEGUNDOS)
        return dados

    def _central(self, request):
        cookie = request.COOKIES.get(settings.CENTRAL_COOKIE_NAME)
        if not cookie:
            return self._ir_para_login(request)
        try:
            dados = self._consultar_central(cookie, getattr(request, "externo", False))
        except (requests.RequestException, ValueError):
            # Falha fechada: sem confirmação da Central, ninguém entra.
            log.error("não foi possível validar a sessão na Central", exc_info=True,
                      extra={"ip": request.ip_cliente})
            return render(request, "conciliacao/bloqueado.html",
                          {"motivo": "Não foi possível confirmar o seu login na Central. Tente de novo em instantes."},
                          status=503)
        if dados is None:
            return self._ir_para_login(request)
        if not dados["permitido"]:
            log.warning("acesso recusado: usuário sem permissão na Central",
                        extra={"ip": request.ip_cliente, "usuario": dados["usuario"]})
            return render(request, "conciliacao/bloqueado.html",
                          {"motivo": "Seu usuário não tem acesso à conciliação. Peça a um administrador da Central."},
                          status=403)
        request.usuario_acesso = {"usuario": dados["usuario"], "nome": dados.get("nome") or dados["usuario"],
                                  "admin": bool(dados.get("admin")),
                                  # tem outros módulos na Central: mostra o link de volta (quem só usa a
                                  # Conciliação não vê a Central)
                                  "ve_central": bool(dados.get("admin") or dados.get("outros_modulos"))}
        self._sincronizar_usuario_django(request)
        return None

    def _sincronizar_usuario_django(self, request):
        """Espelha o usuário da Central num usuário Django, para o admin e a auditoria funcionarem sem senha própria.
        Administrador da Central = staff/superuser no Django. Grava no banco só quando algo muda."""
        dados = request.usuario_acesso
        admin = dados["admin"]
        if (request.user.is_authenticated and request.user.get_username() == dados["usuario"]
                and request.user.is_staff == admin and request.user.is_superuser == admin):
            return
        Usuario = get_user_model()
        usuario, _ = Usuario.objects.get_or_create(username=dados["usuario"])
        alterado = False
        for campo, valor in (("is_staff", admin), ("is_superuser", admin), ("first_name", dados["nome"][:150])):
            if getattr(usuario, campo) != valor:
                setattr(usuario, campo, valor)
                alterado = True
        if usuario.has_usable_password():
            usuario.set_unusable_password()
            alterado = True
        if alterado:
            usuario.save()
        login(request, usuario, backend=_BACKEND_MODELO)
