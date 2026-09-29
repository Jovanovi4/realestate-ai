from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "billing"
    verbose_name = "Тарифы и учёт"

    def ready(self):
        from .signals import connect_usage_signals

        connect_usage_signals()
