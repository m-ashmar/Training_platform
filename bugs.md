# bugs.md — six-dive audit

**All 26 are now fixed — see RESOLUTION at the end of this file.** Findings below are as
found, before any fix. Every finding is either **live** (breaks now) or
**latent** (breaks under a stated future condition), and says how it was verified.

The database used is the development Postgres on `localhost:5433`. It carries test
pollution — 547 of 555 exercises are seeded rows named `Exercise1..405`, `123`, `asd` —
so a finding that depends on row counts says so.

| Dive | Focus | Status |
|---|---|---|
| 1 | Concurrency | done — 9 findings |
| 2 | Permissions by execution | done — 2 findings |
| 3 | Money and webhooks | done — 3 findings |
| 4 | Celery at-least-once | done — 2 findings |
| 5 | Arabic and i18n | done — 6 findings |
| 6 | Volume and N+1 | done — 4 findings |

## DIVE 1 — CONCURRENCY

Method: parallel threads against the development Postgres, plus HTTP through the Django
test client. Verified clean under contention and left alone: `move_funds_atomic` (20
parallel transfers of 10.00 from a wallet holding 100.00 — exactly 10 succeeded, sum
conserved at 100.00, no negative balance), the achievement double-award path (the
unique constraint holds and the caller's transaction survives), the set-log recalc, and
`PaymentService.complete_payment` replay handling.

**[CRITICAL] wallet/views.py:L230 — the idempotency key is global and is never checked against its owner.**
Impact: any client who reuses another client's key receives that client's transfer receipt — reference id and both wallet balances — while their own transfer silently never happens and returns 200.
Verified: Alice posted `client/transfer` with key `key-eac7af`, got `{"reference_id":"755f5168…","client_balance":"475.00","trainer_balance":"25.00"}`. Bob posted the same key for 999.00 and received Alice's response byte for byte. Bob's balance stayed 500.00. Live.
Fix: scope the key to `created_by`, and refuse a key belonging to another user.

**[CRITICAL] wallet/views.py:L230 — `request_hash` is written in four places and compared in none.**
Impact: the same key with a different amount is treated as a replay. Alice repeated her key with 400.00 instead of 25.00 and got 200 with the original 25.00 receipt; no transfer occurred. The column exists precisely to catch this. The transfer and reversal paths store the key as its own hash, so even a comparison would be vacuous.
Verified: probe D above. Live. Same shape at L188, L273, L407.

**[CRITICAL] subscription/permissions.py:L126 — the usage lookup does not match the unique key, and duplicates lock the subscriber out permanently.**
Impact: `get_or_create` looks up `(subscription, feature)` while `unique_together` is `(subscription, feature, period_start)`. Concurrent requests all miss and all insert. From the second row on, `get_or_create` raises `MultipleObjectsReturned`, the bare `except:` at L137 swallows it, and `has_permission` returns False for good. A paying subscriber is silently and permanently denied meal creation and diet plan generation, with nothing logged.
Verified: 8 parallel permission checks produced 8 `SubscriptionUsage` rows with 8 distinct `period_start` values; the next check returned False for a user who had used 0 of 3. Live, on diet/views.py:L728, L785, L833.
Fix: drop `period_start` from the lookup key and make the constraint one the insert can actually violate.

**[HIGH] subscription/models.py — `SubscriptionUsage.unique_together` can never fire.**
Impact: `period_start` is `auto_now_add=True`, so every row gets a distinct value and the constraint rejects nothing. It reads as protection and is not.
Verified: 8 rows inserted under the constraint, 8 distinct `period_start`. Live.

**[HIGH] subscription/utils.py:L159 — `track_feature_usage` has no callers.**
Impact: `usage_count` is never incremented, so `usage.usage_count < limit` is always true and the plan's meal and routine limits are enforced by nothing. The atomic `F()` increment inside it is correct and unreachable.
Verified: `grep -rn track_feature_usage` returns only the definition. Live.
Fix: call it from the endpoints the limits are meant to gate, or delete the limits.

**[MEDIUM] subscription/utils.py:L183 — the limit is read from an attribute the plan does not have.**
Impact: `getattr(subscription.plan, f'max_{feature_name}', 0)` builds `max_daily_meals` for the `daily_meals` feature; the field is `max_meals_per_day`. The fallback is 0, which the caller reads as unlimited. Latent only because nothing calls this function.

**[MEDIUM] subscription/permissions.py:L137 — a bare `except:` on the permission path.**
Impact: catches everything, including `MultipleObjectsReturned` above, and converts it to a denial with no log line. This is what turns the duplicate rows into a silent lockout rather than a 500 someone would notice.

**[MEDIUM] subscription/permissions.py:L119 — a permission check creates rows.**
Impact: `SubscriptionFeature.objects.get_or_create(name=...)` auto-creates a feature from a read path, so an unauthenticated-shaped mistake or a typo in a permission declaration silently populates the feature table. The `except SubscriptionFeature.DoesNotExist` below it can never fire.

**[MEDIUM] subscription/services/payment_service.py:L159 — `start_renewal` has no guard against duplicate pending renewals.**
Impact: nothing stops N pending renewal payments existing for one subscription. Six were created back to back in a probe, and all six completed and charged.
Verified: probe E, 6 pending renewals created and completed, subscription extended 180 days.
Fix: refuse a new renewal while a pending one exists for the same subscription.

**[MEDIUM, latent — not reproduced] subscription/services/payment_service.py:L204 — `_activate_subscription` read-modify-writes the subscription while the lock is on the payment row.**
Impact: two payments for the same subscription each lock their own row, then both read `subscription.end_date` and both write `base + duration`. The later write would overwrite the earlier, so a user who paid twice would get one period.
Verified: **not reproduced.** Four trials at six-way parallelism each credited the full 180 days. The window between the read and the save is narrower than the thread startup cost. Reported as a code shape, not a demonstrated failure.

## DIVE 2 — PERMISSIONS, BY EXECUTION

Method: 147 no-arg API routes requested as anonymous, client, trainer, agent and admin
(735 requests); then a cross-tenant pass where every object was created by trainer A /
client A and fetched by an unrelated trainer B / client B.

Verified clean: `DEFAULT_PERMISSION_CLASSES` is `IsAuthenticated` and no view class
falls through to it by accident. No cross-tenant read succeeded on routines, routine
exercises, exercises, workout sessions, social posts, analytics goals or client
profiles — every one returned 404 to the other tenant. Agent and admin routes are
correctly gated. `update_progress`, `assign_to_client` and `unassign_from_client`
enforce the right actor, checked by executing them as each role.

**[HIGH] users/views.py:L878 — one row with bad data returns 500 for the whole list.**
Impact: `GET /api/auth/trainer/client-profile/` as an admin serialises all 260 clients. Two of them hold `activity_level='moderate'`, and `routine/serializers.py:L688` calls `calculate_daily_calories('Maintain')`, which raises `ValueError: Unrecognised activity level 'moderate'`. The broad handler converts it to a 500 with a generic message, so the cause is invisible in the response and the endpoint is unusable for admins.
Verified: the endpoint returns 500 for admin and 200 for a trainer with no clients; calling the serializer directly surfaces the ValueError. Live.
Root: `activity_level` declares `choices` that only DRF enforces. `CustomUser.save()` does not validate and there is no database constraint, so any writer that is not a serializer — a management command, a fixture, a probe — stores an off-choice value. Migration 0030 cleaned the column once; the two rows present now were written after it ran.
Fix: constrain the column, and make `get_tdee` degrade for one bad row instead of failing the response.

**[MEDIUM] users/views.py — `/api/auth/trainers/stats/` answers anonymously with platform totals.**
Impact: returns `{"clients_with_trainers_count":49,"total_trainers_count":134}` to an unauthenticated caller. Not personal data, but it publishes roster size and adoption to anyone, and it moves with the business.
Verified: anonymous GET returns 200 with that body. Live.

**[INFO] routine/views.py:L408 — checked and clean.**
`RoutineViewSet.get_permissions()` overrides only `list` and `retrieve` and defers to `super()` elsewhere, which does honour `@action(permission_classes=…)`. Executing all three actions as each role confirms the declared permission is the one that runs. Recorded because the same shape at `UserExerciseProgressViewSet` was a real bug, and a static scan cannot tell them apart.

## DIVE 3 — MONEY AND WEBHOOKS

Method: a live ShamCash webhook secret was configured and signed payloads were posted
at `/api/subscription/webhook/shamcash/`.

Verified clean and left alone: HMAC over `<timestamp>.<body>` with `compare_digest`;
a wrong secret, a tampered signature and a timestamp an hour old are each rejected with
400; the endpoint fails closed when no secret is configured; a replayed event id is a
safe no-op that neither double-charges nor extends the subscription; a `failed` webhook
arriving after completion is refused by the payment state machine; `PaymentReconcileView`
is scoped to `subscription__user=request.user` and verifies at the gateway rather than
trusting a status flag.

**[CRITICAL] subscription/views.py:L580 — a refund or chargeback webhook is acknowledged and discarded.**
Impact: `_process_payment_update` branches on success words and failure words and has no `else`. Every other status the gateway can send falls through, the view returns 200, and the gateway never retries. The subscription stays active after the money has gone back.
Verified: with a completed payment and an active subscription, signed webhooks carrying `refunded`, `reversed`, `chargeback`, `cancelled` and `disputed` each returned HTTP 200 with `payment=completed subscription=active`. Live.
Note the model already declares the transition `'completed': {'refunded'}` at subscription/models.py:L21, and nothing in the request path can reach it.
Fix: handle refund and reversal statuses, and return a non-200 for a status the handler does not recognise so the gateway retries rather than assuming success.

**[HIGH] subscription/admin.py:L120, L124, L128 — three admin actions write payment status with `queryset.update()`.**
Impact: `.update()` bypasses `save()`, so it bypasses the state machine that subscription/models.py:L14 declares is the only legal way to move a payment, and it bypasses `validate_row()`. Concretely: `mark_as_completed` sets a payment completed without activating the subscription, so the customer pays and receives nothing; `mark_as_refunded` returns the money without revoking access; `mark_as_failed` can move a completed payment to failed, which the transition map forbids.
Verified: read of subscription/admin.py; the three actions are plain `queryset.update(status=...)` calls with no service involvement. Live whenever an admin uses them.
Fix: route all three through `PaymentService`.

**[MEDIUM] subscription/views.py:L829 — `PaymentReconcileView` has no throttle.**
Impact: an authenticated user can call reconcile repeatedly on their own pending payment, and each call makes an outbound gateway lookup. `DEFAULT_THROTTLE_RATES` declares a `charging` scope and only wallet/views.py uses it; the subscription app declares none.
Verified: no `throttle_classes` or `throttle_scope` anywhere under `subscription/`.

## DIVE 4 — CELERY, AT-LEAST-ONCE

Method: every beat entry compared against the registered task list, then each of the
nine scheduled tasks executed twice with a full row-count snapshot of all 71 models
taken before and after each run.

Verified clean and left alone: all nine beat entries resolve to registered tasks, so
the "beat skips an unknown name silently" failure is closed. All nine are idempotent —
the second run wrote nothing in every case, including the retention purge and
subscription expiry. `CELERY_TASK_ACKS_LATE`, `CELERY_TASK_REJECT_ON_WORKER_LOST`,
`CELERY_WORKER_PREFETCH_MULTIPLIER = 1` and both time limits are set, so delivery really
is at-least-once and a killed worker redelivers rather than dropping. Retry policies are
narrow (`TRANSIENT_ERRORS`) and bounded at three with backoff. Tasks that take a
`user_id` handle the user having been deleted.

Side effect worth recording: running `purge_expired_personal_data` deleted 150 rows past
their retention window (77 notifications, 35 idempotency keys, 20 OTP verifications, 17
activity logs, 1 session). Correct behaviour; it had simply never run on this database.

**[MEDIUM, latent] achievements/engine.py:L407 and users/views.py:L1088 — an event is queued inside an open transaction.**
Impact: `emit_event` reaches `process_event_task.delay()` with no `transaction.on_commit`, so the broker has the message before the transaction commits. A worker can pick it up and read state that is not yet visible, and if the transaction rolls back the notification is still delivered — the user is told they earned an achievement, or that a trainer request arrived, when neither was saved.
Verified: an AST walk of the eight files that emit events found exactly these two inside an open atomic block. Latent in development only because `settings_local` sets `CELERY_TASK_ALWAYS_EAGER = True`; `settings_production` sets it False.
Fix: wrap both in `transaction.on_commit`, as routine/views.py:L1180 already does.

**[MEDIUM] notifications/tasks.py:L119 and L95 — two tasks carry no retry policy.**
Impact: `send_workout_reminders` is the daily beat job and `award_progress_milestones` is dispatched after every finished workout. Both are bare `@shared_task(name=...)` while every other task in the codebase has `autoretry_for`, backoff and `max_retries=3`. A transient Redis or database blip drops that day's entire reminder run, or one user's milestone, with only a log line.

## DIVE 5 — ARABIC AND i18n

Method: every endpoint class driven twice, once with `Accept-Language: en` and once
with `ar`, and the two responses diffed; then the Arabic fill rate measured on every
field registered with modeltranslation.

Verified clean and left alone: `LanguageResolutionMiddleware` resolves the header and
the JWT's `preferred_language` and activates it; DRF's own 401, 403 and 400 messages
come back in Arabic; `notifications/channels/fcm.py` is a properly built delivery-time
translation boundary that resolves the recipient's *current* language via
`LanguageContext.for_user_id`.

**[HIGH] Arabic content is empty across every model that declares it.**
Impact: the API serves `ar` and returns English for all of it. An Arabic client sees an English app.
Verified: counted directly against the database.

| Field | Filled |
|---|---|
| routine.Exercise.name_ar | 1 of 558 |
| routine.Exercise.description_ar | 1 of 558 |
| diet.FoodItem.name_ar | 0 of 346 |
| diet.FoodCategory.name_ar | 0 of 11 |
| social.Challenge.title_ar | 0 of 14 |
| diet.DietPlanTemplate.name_ar | 0 of 4 |
| achievements.Achievement.name_ar | 2 of 20 |
| routine.RoutineTemplate.name_ar | 5 of 44 |

Content, not code, but it is the difference between a bilingual product and an English one.

**[HIGH] subscription/models.py — `SubscriptionPlan` is not registered for modeltranslation.**
Impact: there is no `name_ar` or `description_ar` to fill. The plan list is what a user reads immediately before paying, and it has no Arabic path at all. `SubscriptionFeature` and `analytics.UserGoal` are likewise unregistered.
Verified: `GET /api/subscription/v1/plans/` returns byte-identical bodies under `en` and `ar`; `translator.get_registered_models()` lists eight models and none of them is in the subscription app. Live.

**[MEDIUM] notifications/milestones.py:L24 — milestone text is rendered in the wrong language.**
Impact: `evaluate()` uses eager `gettext` and runs on the Celery worker, where no language is active, so the sentence resolves against `LANGUAGE_CODE = "en"`. It is then stored in `metadata.context.message` and interpolated into the template by the FCM boundary, which translates the frame around an already-English sentence. An Arabic user's milestone push reads as Arabic wrapping English.
Verified: the string is built at notifications/milestones.py:L36-L51 and passed to `NotificationService.create_and_send` at L97 with no `LanguageContext`. Live.
Fix: use `gettext_lazy`, or build the message inside `LanguageContext.for_user_id`.

**[MEDIUM] 404 bodies are not translated.**
Impact: `GET /api/routine/routines/99999999/` returns `"No Routine matches the given query."` under `Accept-Language: ar`, where 401, 403 and 400 all come back in Arabic. The message is generated by `get_object_or_404` with the model name interpolated, so it never reaches the catalogue.
Verified: probe above. Live.

**[MEDIUM] locale/ar/LC_MESSAGES/django.po — 113 of 409 entries have no translation.**
Impact: 28% of the platform's own translatable strings fall back to English at runtime, silently.
Verified: `grep -c '^msgstr ""'` on the catalogue.

**[LOW] choice labels are returned raw.**
Impact: `target_muscle: "Front Quads"` and `difficulty_level: "beginner"` are serialised as stored values rather than localised labels, so they stay English under `ar`.

## DIVE 6 — VOLUME AND N+1

Method: seeded a trainer and client with 5 of each resource, measured queries per
endpoint, seeded to 25, measured again. Query counts read from `connection.queries`
after the response rather than through `CaptureQueriesContext` (see the last finding).

Verified clean and left alone: routines (trainer and client views), exercises,
routine-exercises, workout sessions, the social feed and subscriptions are all flat as
rows grow from 5 to 25 — the `select_related`/`prefetch_related` on those querysets
holds. Cursor pagination on the notification list returns genuinely different pages
when the `next` link is followed.

**[HIGH] achievements/views.py — the achievement list costs four queries per achievement.**
Impact: linear in catalogue size on a screen the mobile client opens routinely. `get_user_progress` is called per achievement and each call re-checks whether it is earned and recomputes its metric.
Verified: with 20 achievements the endpoint issues 70 queries; adding 20 more takes it to 150, exactly 4.0 per achievement. The repeated shapes are 74 × `EXISTS UserAchievement`, 37 × `SELECT UserAchievement` and 25 × `COUNT analytics_useractivity`. A realistic 100-achievement catalogue would be roughly 400 queries per open. Live.
Fix: fetch the user's earned set once, and batch the metric counts.

**[HIGH] social — the notification list does one user lookup per notification.**
Impact: `GET /api/social/notifications/` issues 21 identical `SELECT … FROM users_customuser` for a page of 20. The serializer resolves a user per row with no `select_related`. It does not grow with the total row count because the page is capped, but it multiplies every notification screen by the page size.
Verified: 22 queries for 20 rows, 21 of them the same user select. Live.

**[MEDIUM] social — the notification list ignores `?page=` and omits `count`.**
Impact: it uses cursor pagination, so `?page=2` silently returns page 1 again, and the envelope is `{next, previous, results}` with no `count`. settings_base.py:L198 states the platform has one pagination shape, `{count, next, previous, results}`, and every other list endpoint returns it. A client written against that contract pages forever through the same twenty rows and cannot show a total.
Verified: `?page=2` returned byte-identical first ids to page 1; following the `next` cursor link worked. Live.

**[MEDIUM] training_platform/middleware.py:L399 — the query-count middleware makes query-count tests impossible.**
Impact: `DatabaseQueryCountMiddleware.process_request` calls `reset_queries()` on every request, which clears the log that `CaptureQueriesContext` reads. Any `assertNumQueries` around a test-client request therefore observes zero queries and passes, so the project cannot write a regression test for either N+1 above. It cost this dive one wasted measurement pass, which read 0 queries for every endpoint.
Verified: the first measurement returned 0 queries for eight of nine endpoints; reading `connection.queries` after the response instead returned the real counts.

---

## SUMMARY

26 findings. Nothing was fixed; the working tree is unchanged except for this file.

| Severity | Count | Where |
|---|---|---|
| CRITICAL | 4 | wallet idempotency (2), subscription usage quota, refund webhook |
| HIGH | 8 | quota constraint, usage tracker, admin payment actions, list 500, Arabic content, plan translation, two N+1s |
| MEDIUM | 13 | spread across all six dives |
| INFO | 1 | a static-scan false positive, recorded so it is not re-raised |

**The four to fix first**, all money or access:

1. wallet/views.py:L230 — one client reads another's transfer receipt through a shared idempotency key, and their own transfer silently does not happen.
2. wallet/views.py:L230 — `request_hash` is written four times and compared never, so the same key with a different amount replays the old receipt.
3. subscription/permissions.py:L126 — concurrent requests create duplicate usage rows and then lock the paying subscriber out of diet and meal endpoints permanently and silently.
4. subscription/views.py:L580 — a refund or chargeback webhook is answered 200 and dropped; the money goes back and the subscription stays active.

**The recurring shape**, in this dive as in the last one: something is declared and
honoured by nothing. `request_hash` written and never compared. `track_feature_usage`
written and never called. A `unique_together` whose third column is `auto_now_add`, so
it can never fire. A `'completed' -> 'refunded'` transition no request path can reach.
`SubscriptionPlan` serving a bilingual API with no translated fields. A pagination
contract stated in settings that one endpoint does not keep. Worth making the next dive
a systematic sweep for it rather than finding them one at a time.

**Two things this database cannot tell you.** It is a development database: 547 of 555
exercises are seeded test rows, so anything counted here is about shape, not scale. And
the Arabic gap is content, not code — the columns exist and resolve correctly, they are
simply empty.

**Not covered by these six dives**, if you want more: WebSocket connection auth and
group membership; file upload and media access control; cache correctness across the
six Redis databases; the OTP and registration flow under contention; and a run against
a database seeded the way production will actually be seeded.

---

# RESOLUTION — all 26 fixed, 2026-09-03

Every finding above is closed. The gate is at 85 tests, `manage.py check` is clean, all
4,819 rows across the twelve validated models still pass, and no migration is pending.
Fixes were grouped by root cause, not addressed one at a time.

## Root A — idempotency was a global token, not a scoped, content-bound record
Fixes findings 1 and 2. Four money endpoints each held their own copy of the same eight
lines, and every copy shared both holes. `wallet/idempotency.py` is now the only
implementation: keys are unique per caller (migration 0007), and each carries a digest
of the request that claimed it, so a replay with different content is a 422 rather than
the earlier request's receipt. Rows that stored the key as its own hash are blanked and
treated as legacy: replayable, never compared.
*Verified:* Bob no longer receives Alice's receipt; a key replayed with 400.00 instead
of 25.00 is refused and no money moves.

## Root B — no single owner for "has this subscriber any left, and record that they used some"
Fixes findings 3 through 8. The answer was split between a permission class that created
rows and never counted, and an uncalled utility that resolved the limit from a field the
plan does not have. `subscription/quota.py` owns all of it. `period_start` is a computed
window boundary instead of `auto_now_add`, which is what finally makes the unique key
able to fire; the three diet endpoints that declare the limit now spend it on commit;
`SubscriptionFeature` rows are seeded by migration 0008 rather than invented by a read
path; the bare `except:` is gone.
*Verified:* eight parallel checks now produce one usage row and eight allowances, where
they produced eight rows and then a permanent lockout. The limit stops at 3 of 3, and
ten parallel increments land as ten.

**One thing this uncovered.** `validate_row()` was pre-checking uniqueness, and
`get_or_create` resolves a lost race by catching `IntegrityError` — which a
`ValidationError` raised first is not. That denied half the concurrent callers. The
model mixin no longer pre-checks either database rule; DRF's `UniqueTogetherValidator`
still returns 400 for API writes, and the database has the last word everywhere else.

## Root C — payment state changed outside the service declared to own it
Fixes findings 13, 14, 9 and 10. `PaymentService.refund_payment` exists and withdraws
the access the payment bought. The webhook handler is exhaustive: an unrecognised status
is a 400 so the gateway retries, instead of a 200 that told it the message landed.
The three admin actions route through the service rather than `queryset.update()`.
`start_renewal` returns the pending renewal instead of opening another, and
`_activate_subscription` takes the subscription's own row lock.
*Verified:* signed `refunded`, `reversed`, `chargeback`, `cancelled` and `disputed`
webhooks each leave the subscription cancelled; `banana` and an empty status return 400;
five `start_renewal` calls return one payment.

## Root D — a form-layer rule guarding a value the calculation refuses
Fixes finding 11. `activity_level` declared `choices` that only DRF enforced, and
`calculate_daily_calories` raises on anything else — from a serializer method field
called once per row. Migration 0031 normalises and constrains the column, and `get_tdee`
returns 0.0 with a log line for a profile it cannot compute.
*Verified:* the client-profile list returns 200 with an incomplete profile in it, and
the database refuses `'moderate'`.

## Root E — work queued inside an open transaction
Fixes finding 16. Both `emit_event` calls that ran inside an atomic block are wrapped in
`transaction.on_commit`, so nothing is queued before the row it describes exists and
nothing is delivered after a rollback.

## Root F — the bilingual paths that were not bilingual
Fixes findings 18 through 23. `SubscriptionPlan` is registered for translation with its
English text backfilled (migrations 0009, 0010). Milestone text is rendered inside
`LanguageContext.for_user_id` from lazy strings, so an Arabic user's push is Arabic
throughout rather than an Arabic frame around an English sentence. 404 bodies use DRF's
translated sentence. Muscle and difficulty labels are marked for translation and served
as `target_muscle_display` / `difficulty_level_display`. **The Arabic catalogue is
complete: 481 of 481 entries, up from 296.** A native speaker should still review the
174 strings written here.
*Verified:* `target_muscle_display` returns `الرباعية الأمامية`; a 404 under
`Accept-Language: ar` returns `غير موجود.`

The empty `name_ar` on 346 foods and 554 exercises is content, not code. Those columns
resolve correctly and are simply unwritten; nothing here invented catalogue data.

## Root G — per-row work in serializers
Fixes findings 24 and 25. The achievement list builds the earned map and a metric memo
once per request instead of three queries per achievement; the notification queryset
joins `recipient`.
*Verified:* achievements went from 4.0 queries per achievement to 0.0 — flat at 16
whether the catalogue is 40 or 60. The notification list went from 22 queries to 2.

## Root H — a pagination contract one endpoint did not keep
Fixes finding 26. Cursor pagination stays, because the reasons in its docstring are
good. `?page=` is now a 400 that says to follow the `next` link, rather than page one
returned forever, and the contract comment in settings names the exception.

## Root I — the middleware that made query counting untestable
Fixes finding 27. `DatabaseQueryCountMiddleware` records an offset instead of calling
`reset_queries()`, so it still counts what it counted and `assertNumQueries` works
again. Both N+1s above now have regression tests that would have caught them.

## The remaining three
- **12** — `/api/auth/trainers/stats/` requires authentication.
- **15** — `PaymentReconcileView` carries the `charging` throttle, which the wallet was
  already using and the subscription app declared and never applied.
- **17** — the two beat tasks that had no retry policy now share the one every other
  task in the codebase uses.

## Migrations added
`wallet/0007`, `subscription/0008`, `subscription/0009`, `subscription/0010`,
`users/0031`.

## Tests added
Fourteen, covering every root above: cross-caller idempotency, content-bound replay,
quota counting and lockout, refund revocation, the renewal guard, one bad row not
failing a list, the activity-level constraint, 404 translation, catalogue completeness,
both N+1s, the query log itself, and the pagination contract.

---

# DIVES 7-10 — the four areas that had never been audited (2026-09-04)

Run after the resolution above, against the same development Postgres. One finding.

## DIVE 7 — CACHE CORRECTNESS: CLEAN
Two layers, both checked by execution. The HTTP response cache keys on the caller's
verified JWT subject for `private` routes and omits identity only for `public` ones,
and the registry that decides which is which is a single file both the middleware and
the invalidation signals read. Trainer A's exercise catalogue was never served to
trainer B. English and Arabic hold separate entries, which matters now that plans are
translatable. A write bumped the version counter and the next read saw it. Cachalot's
uncachable set covers every wallet, subscription, auth and OTP table, including the
usage table repaired above.

## DIVE 8 — WEBSOCKETS: CLEAN
Both consumers reject a missing token, a refresh token and a malformed token, and
accept only an access token — the `UntypedToken` hole `ws_auth.py` was written to close
is genuinely closed. The AI socket refuses a caller with no live entitlement (4003) and
re-checks it per message, so a lapsed subscription cannot keep spending model tokens on
an open socket. Passing another user's `session_id` gets you your own session, because
`_get_or_create_session` scopes the lookup by user. The social consumer derives its
group from the token, so there is no frame a client can send to subscribe to someone
else's stream.

## DIVE 9 — UPLOADS AND MEDIA: CLEAN
Five traversal shapes all 404. Unsigned and tampered media links 404; a correctly
signed one returns 200, and a signature minted for one path does not open another.
Anything that is not an inline-safe type is forced to `Content-Disposition: attachment`,
so an uploaded file cannot execute in a browser. PHP disguised as a JPEG, an SVG
carrying a script, HTML named `.png` and executable bytes named `.png` are all rejected
by `SecureImageField`, and a genuine 1×1 PNG passes.

*Worth knowing, not a defect:* a signed media URL is a bearer token. Anyone holding the
link can fetch the file for `MEDIA_URL_TTL` (24 hours), regardless of who they are.

## DIVE 10 — AUTH, OTP AND REGISTRATION: one finding

**[HIGH] templates/emails/otp_verification.html:L99, L109 and password_reset_otp.html:L105, L109 — every account email went out with template syntax in it. FIXED.**
Impact: Django's `tag_re` is compiled without `re.DOTALL`, so a `{% ... %}` that does
not close on the same line is never recognised as a tag — it is emitted as literal text.
Four `{% trans %}` tags were wrapped across lines, so the HTML body of every
registration and every password-reset email contained `{% trans "Thank you for
registering with Training Platform! …" %}` verbatim. Those four sentences also never
reached the translation catalogue.
Verified: capturing a real registration email showed `{% trans` present in the
`text/html` part and absent from the plain-text part — which is why nothing about it
was visible from the API. Live, and on the first screen a new user ever sees.
Fixed by joining the four tags onto one line; the four sentences are now in the
catalogue and translated. A gate test now sweeps every template for a tag left open at
a line end.

Everything else in this flow held: registration creates an inactive user and a hashed
OTP; `admin` and `agent` cannot be self-registered; five wrong codes trigger a
15-minute lockout that also refuses the correct code; a correct code first time
activates the account and returns tokens; neither login nor password reset reveals
whether an email is registered; six parallel registrations for one email produce one
account.

*Product decision, not a defect:* `trainer` is self-registerable. Only `admin` and
`agent` require a superuser.
