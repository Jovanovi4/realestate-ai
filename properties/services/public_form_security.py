import logging

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils.crypto import salted_hmac


logger = logging.getLogger(__name__)
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class PublicLeadFormSecurity:
    """Layered, fail-closed protections for public landing lead submissions."""

    @classmethod
    def client_ip(cls, request):
        if settings.PUBLIC_FORMS_TRUST_X_FORWARDED_FOR:
            forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
            if forwarded_for:
                return forwarded_for.split(",", 1)[0].strip()
        return request.META.get("REMOTE_ADDR", "unknown")

    @classmethod
    def allow_submission(cls, request, property_id):
        ip_address = cls.client_ip(request)
        global_key = cls._rate_limit_key("all", ip_address)
        property_key = cls._rate_limit_key(f"property:{property_id}", ip_address)
        return cls._consume(global_key, settings.PUBLIC_LEAD_RATE_LIMIT) and cls._consume(
            property_key, settings.PUBLIC_LEAD_RATE_LIMIT_PER_PROPERTY
        )

    @staticmethod
    def _rate_limit_key(scope, ip_address):
        fingerprint = salted_hmac("public-lead-rate-limit", f"{scope}:{ip_address}").hexdigest()
        return f"public-lead-rate:{scope}:{fingerprint}"

    @staticmethod
    def _consume(key, limit):
        if limit <= 0:
            return True
        window = settings.PUBLIC_LEAD_RATE_WINDOW_SECONDS
        if cache.add(key, 1, timeout=window):
            return True
        try:
            return cache.incr(key) <= limit
        except ValueError:
            cache.set(key, 1, timeout=window)
            return True

    @classmethod
    def verify_turnstile(cls, request):
        if not settings.TURNSTILE_ENABLED:
            return True
        token = request.POST.get("cf-turnstile-response", "")
        if not token:
            return False
        try:
            response = requests.post(
                TURNSTILE_VERIFY_URL,
                data={
                    "secret": settings.TURNSTILE_SECRET_KEY,
                    "response": token,
                    "remoteip": cls.client_ip(request),
                },
                timeout=5,
            )
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError):
            logger.exception("Turnstile validation request failed")
            return False
        if not result.get("success"):
            return False
        if settings.TURNSTILE_ALLOWED_HOSTNAMES:
            return result.get("hostname", "").lower() in settings.TURNSTILE_ALLOWED_HOSTNAMES
        return True
