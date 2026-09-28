"""Shared throttling for public authentication endpoints, including admin login."""

import ipaddress
import logging
import re

from django.conf import settings
from django.core.cache import cache
from django.utils.crypto import salted_hmac


logger = logging.getLogger(__name__)


def normalized_login_identity(value):
    identity = re.sub(r"[\s()\-]", "", value.strip())
    if identity.startswith("00"):
        identity = "+" + identity[2:]
    return identity.casefold()[:254]


class AccountRateLimiter:
    @staticmethod
    def client_ip(request):
        value = request.META.get("REMOTE_ADDR", "")
        if settings.AUTH_TRUST_X_FORWARDED_FOR:
            value = request.META.get("HTTP_X_FORWARDED_FOR", value).split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            return "unknown"

    @staticmethod
    def key(action, scope, value):
        digest = salted_hmac("account-rate-limit", f"{action}:{scope}:{value}").hexdigest()
        return f"account-rate:{action}:{scope}:{digest}"

    @staticmethod
    def consume(key, limit, window):
        # add/incr are atomic on the production Redis backend. Do not reset an
        # existing window on each attempt or fail open when the cache is down.
        for _ in range(2):
            if cache.add(key, 1, timeout=window):
                return True
            try:
                return cache.incr(key) <= limit
            except ValueError:
                continue  # The key may have expired between add() and incr().
        return False

    @classmethod
    def allow(cls, request, action, identity=""):
        policy = settings.AUTH_RATE_LIMITS[action]
        ip = cls.client_ip(request)
        scopes = [("ip", ip, policy["ip"])]
        if identity and policy.get("pair"):
            scopes.append(("pair", f"{ip}:{identity}", policy["pair"]))
        if identity and policy.get("identity"):
            scopes.append(("identity", identity, policy["identity"]))
        try:
            allowed = [cls.consume(cls.key(action, scope, value), limit, policy["window"]) for scope, value, limit in scopes]
            return all(allowed)
        except Exception:
            logger.exception("Account rate-limit cache is unavailable")
            return False
