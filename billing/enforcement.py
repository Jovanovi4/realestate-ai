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
    if (enabled() or decision.code == "team_closed") and not decision.allowed:
        raise TariffDenied(decision)
    return decision


def require_resource_action(actor, resource, operation):
    """Check a saved record against its own workspace, not the actor's default one."""
    if getattr(resource, "agency_id", None) and resource.agency.closed_at:
        from .rules import PolicyDecision
        raise TariffDenied(PolicyDecision(False, "team_closed", "Команда закрыта. Данные доступны владельцу в архиве."))
    if not enabled():
        return None
    from .rules import PolicyService
    from .services import AccountService

    account = AccountService.ensure_for_resource(resource)
    return require_allowed(PolicyService.evaluate(account, actor, operation, resource=resource))


def telegram_publication_decision(actor, property):
    """The Telegram feature is checked even while general quota enforcement is off."""
    from .rules import Operation, PolicyService
    from .services import AccountService

    account = AccountService.ensure_for_resource(property)
    return PolicyService.evaluate(account, actor, Operation.PUBLISH_TELEGRAM, resource=property)


def require_telegram_publication(actor, property):
    decision = telegram_publication_decision(actor, property)
    if not decision.allowed:
        raise TariffDenied(decision)
    return decision


def require_new_action(actor, operation, *, agency=None):
    """Check an unsaved CRM record in the workspace it will be created in."""
    if agency and agency.closed_at:
        from .rules import PolicyDecision
        raise TariffDenied(PolicyDecision(False, "team_closed", "Команда закрыта. Данные доступны владельцу в архиве."))
    if not enabled():
        return None
    from .rules import PolicyService
    from .services import AccountService

    account = AccountService.ensure_agency(agency) if agency else AccountService.ensure_personal(actor)
    return require_allowed(PolicyService.evaluate(account, actor, operation))
