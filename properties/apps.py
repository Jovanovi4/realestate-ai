from django.apps import AppConfig


class PropertiesConfig(AppConfig):
    name = 'properties'

    def ready(self):
        from .audit import connect_audit_signals

        connect_audit_signals()
