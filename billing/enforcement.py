"""One switch and one exception for server-side tariff decisions."""

from django.conf import settings


class TariffDenied(Exception):
    def __init__(self, decision):
        self.decision = decision
        self.code = decision.code
        self.message = decision.message
        super().__init__(self.message)


def enabled():
    return getattr(settings, "BILLING_ENFORCEMENT_ENABLED", False)


def require_allowed(decision):
    if enabled() and not decision.allowed:
        raise TariffDenied(decision)
    return decision


def require_resource_action(actor, resource, operation):
    """Check a saved record against its own workspace, not the actor's default one."""
    if not enabled():
        return None
    from .rules import PolicyService
    from .services import AccountService

    account = AccountService.ensure_for_resource(resource)
    return require_allowed(PolicyService.evaluate(account, actor, operation, resource=resource))


def require_new_action(actor, operation, *, agency=None):
    """Check an unsaved CRM record in the workspace it will be created in."""
    if not enabled():
        return None
    from .rules import PolicyService
    from .services import AccountService

    account = AccountService.ensure_agency(agency) if agency else AccountService.ensure_personal(actor)
    return require_allowed(PolicyService.evaluate(account, actor, operation))
