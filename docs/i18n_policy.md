# Multilingual Architecture Policy

> This is **enforced system policy**, not a suggestion.
> All engineers must follow these rules when touching notification, cache, async, or API serialization code.

---

## 1. Delivery-Time Language Policy

**Rule:** Notification language is determined at **delivery time**, not at event emission time.

- The recipient's `preferred_language` is fetched from the DB inside the delivery channel (FCM, email, WebSocket).
- The payload-time language is **ignored**.
- If a user changes their language between event emission and delivery, they receive the notification in their **new** language.

**Implementation:** `LanguageContext.for_user_id(recipient.id)` performs a fresh DB lookup.

---

## 2. Translation Boundary Discipline

**Rule:** Translation must happen only at the **final delivery boundary**.

- Celery payloads must contain `event_type` + primitive context data — **never** rendered strings.
- Cache entries must contain raw data — **never** pre-translated values.
- Notification model stores `event_type` + `metadata` (JSON) — **never** `title` or `body`.
- FCM channel, email renderer, and WebSocket consumer are the only places where `str(_(…))` is evaluated.

---

## 3. Serialization Safety

All serialized data across process boundaries must be **primitive-only**:

```python
# ✅ Correct
{"event_type": "post_liked", "payload": {"actor_id": 42, "post_id": 99}}

# ❌ Wrong
{"title": "Ahmed liked your post", "body": "..."}  # English string leak
```

Never:
- Pickle event objects
- Serialize `gettext_lazy` objects
- Serialize `NotificationTemplate` instances

---

## 4. ASGI / Async Language Safety

- **Never** call `translation.activate()` in ASGI consumers or at module import time.
- **Always** use `LanguageContext.for_user()` as a context manager per handler invocation.
- `translation.override()` is coroutine-safe; `translation.activate()` is **not** (threadlocal leaks).

---

## 5. Cache Language Partitioning

All cache keys that contain user-visible translated data **must** include:

```
{domain}:{entity}:{id}:{lang}:{CACHE_VERSION}
```

- `CACHE_VERSION` is defined in `training_platform/i18n.py`.
- Bump `CACHE_VERSION` whenever serializer structure or translated fields change.
- Cache invalidation must loop over all `settings.LANGUAGES` entries.

---

## 6. Validators and Error Messages

- All `ValidationError` messages **must** use `gettext_lazy` (`_()`).
- All `ValidationError` calls **must** include a `code=` parameter.
- Never expose raw exception messages or f-strings with internal values to API responses.

---

## 7. Search & Indexing Strategy (Future)

When implementing search (Elasticsearch, vector DB, full-text):

- Use language-specific analyzers (Arabic analyzer for `ar` fields, standard for `en`).
- Index translatable fields per language (e.g., `name_en`, `name_ar` as separate index fields).
- Never index only the default language field.

---

## 8. Observability

Three Prometheus counters track i18n health:

| Counter | Meaning |
|---------|---------|
| `notification_template_missing_total` | Events without a bound template |
| `notification_context_error_total` | Template renders with invalid context |
| `notification_language_fallback_total` | Invalid stored languages that triggered fallback |

Alert thresholds should be set on these counters in production dashboards.
