from django.apps import AppConfig


class RoutineConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "routine"

    def ready(self):
        # Registers the pre_delete receiver that hands a deleted trainer's exercises
        # to the platform. Without it, Django's own SET_NULL pass writes half of a
        # derived pair and the visibility constraint rejects the delete.
        from routine import signals  # noqa: F401
