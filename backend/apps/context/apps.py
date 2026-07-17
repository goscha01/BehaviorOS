from django.apps import AppConfig


class ContextConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.context'

    def ready(self):
        # Import concrete sources so their @register decorators run at
        # startup. Without this, ContextEngine finds an empty registry.
        from apps.context import sources  # noqa: F401
