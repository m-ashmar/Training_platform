from prometheus_client import Counter

# Events
events_emitted_total = Counter(
    'notification_events_emitted_total',
    'Total number of domain events emitted',
    ['event_type']
)

# Notifications
notifications_created_total = Counter(
    'notifications_created_total',
    'Total number of notifications created in DB',
    ['event_type']
)

notifications_deduplicated_total = Counter(
    'notifications_deduplicated_total',
    'Total number of duplicate notifications suppressed',
    ['source', 'event_type']  # source: redis, db
)

# Channels (FCM)
fcm_sent_total = Counter(
    'notification_fcm_sent_total',
    'Total number of FCM messages successfully sent',
    ['event_type']
)

fcm_failed_total = Counter(
    'notification_fcm_failed_total',
    'Total number of FCM messages failed',
    ['event_type', 'error_code']
)

invalid_tokens_total = Counter(
    'notification_invalid_tokens_total',
    'Total number of tokens marked as invalid',
    ['platform']
)

# ── i18n observability ──

notification_template_missing_total = Counter(
    'notification_template_missing_total',
    'Events processed without a bound NotificationTemplate',
    ['event_type']
)

notification_context_error_total = Counter(
    'notification_context_error_total',
    'Template renders that failed due to missing/invalid context keys',
    ['event_type', 'error_kind']
)

language_fallback_total = Counter(
    'notification_language_fallback_total',
    'Times a stored preferred_language was invalid and fell back to LANGUAGE_CODE',
    ['invalid_value']
)
