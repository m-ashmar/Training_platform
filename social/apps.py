from django.apps import AppConfig


class SocialConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'social'
    verbose_name = 'Social Features'
    
    def ready(self):
        # Import signal handlers (cache-version bumping + recent-progress busting).
        # Imported explicitly: a silent `except ImportError: pass` here would hide a
        # broken signals module and cause stale caches with no error anywhere.
        import training_platform.signals  # noqa: F401
        # Challenge progress counted from logged training: receivers on the routine
        # app's set, progress and session rows (BACKEND_BUGS B-075).
        import social.challenge_progress  # noqa: F401
