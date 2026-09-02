from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'analytics'
    verbose_name = 'Analytics'

    def ready(self):
        # Imported explicitly, NOT inside try/except ImportError — this app previously
        # swallowed the import of a module that did not exist, which was a permanent
        # silent no-op. A broken import here must surface.
        #
        # These receivers are what make the analytics tables actually get written. The
        # models were read in 37 places and written by the server in none, so the
        # achievement criteria computed from UserActivity, PerformanceMetric and
        # UserGoal could never be met unless the mobile client posted the rows itself.
        import analytics.signals  # noqa: F401
