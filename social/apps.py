from django.apps import AppConfig


class SocialConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'social'
    verbose_name = 'Social Features'
    
    def ready(self):
        # Import signal handlers
        try:
            import social.signals
        except ImportError:
            pass 