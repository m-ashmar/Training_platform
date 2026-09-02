from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "users"

    def ready(self):
        # Connect user lifecycle receivers. Without this import the handlers in
        # users/signals.py are never registered.
        import users.signals  # noqa: F401
