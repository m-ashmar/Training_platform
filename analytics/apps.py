from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'analytics'
    verbose_name = 'Analytics'
    
    def ready(self):
        # Import signal handlers
        try:
            import analytics.signals
        except ImportError:
            pass 