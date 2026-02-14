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
