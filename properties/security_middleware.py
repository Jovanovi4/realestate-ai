from django.conf import settings
from django.shortcuts import render

from .services.account_security_service import AccountRateLimiter, normalized_login_identity


class AccountSecurityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        match = request.resolver_match
        if match and match.url_name in {"password_reset_confirm", "account_email_confirm"}:
            # Never expose token paths in Referer, but retain the site origin.
            # no-referrer turns Origin into "null" on form POSTs and breaks CSRF.
            response["Referrer-Policy"] = "strict-origin"
            response["Cache-Control"] = "no-store"
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.method != "POST":
            return None
        match = request.resolver_match
        name = match.view_name
        identity = ""
        if name in {"login", "admin:login"}:
            action = "login"
            identity = normalized_login_identity(request.POST.get("username", ""))
        elif name == "register":
            action = "register"
            identity = normalized_login_identity(request.POST.get("phone", ""))
        elif name == "password_reset":
            action = "reset"
            identity = request.POST.get("email", "").strip().casefold()[:254]
        elif name == "account_email_verify":
            action = "verify"
            identity = str(request.user.pk) if request.user.is_authenticated else ""
        elif name in {"password_reset_confirm", "account_email_confirm"}:
            action = "confirm"
        else:
            return None
        if AccountRateLimiter.allow(request, action, identity):
            return None
        response = render(request, "registration/account_security.html", {
            "title": "Попробуйте немного позже",
            "description": "Слишком много попыток или проверка безопасности временно недоступна. Подождите и повторите действие.",
            "link_name": "login", "link_label": "Вернуться ко входу",
        }, status=429)
        response["Retry-After"] = str(settings.AUTH_RATE_LIMITS[action]["window"])
        response["Cache-Control"] = "no-store"
        return response
