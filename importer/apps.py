# Classification: INTERNAL

from django.apps import AppConfig


class ImporterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "importer"

    def ready(self) -> None:
        from importer import signals  # noqa: F401  (registers auth audit receivers)
