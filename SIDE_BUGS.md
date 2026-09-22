# Side bugs — found while building, fixed later

Anything noticed in passing that is **not** part of the task in hand goes here instead of
derailing it. One row per finding, newest at the top of its section. Fix them in a pass of
their own, then move the row to **Closed** with the commit that closed it.

Not for: work the current task owns, bugs the app session reports (that is
`BACKEND_BUGS.md`), or ideas — this is defects only.

| Field | Meaning |
|---|---|
| ID | `S-001`, in order |
| Where | `file.py:L42`, or the endpoint |
| What | one line: what is wrong, not what to do |
| Impact | who sees it go wrong |
| Sev | CRITICAL / HIGH / MEDIUM |
| Found | the build step that walked past it |

## Open

| ID | Where | What | Impact | Sev | Found |
|---|---|---|---|---|---|
| S-055 | `routine/views.py:22` | `WorkoutSessionDetailSerializer` and `ClientProfileViewSerializer` are imported and used nowhere | The same dead-import defect as the just-closed S-037, on the adjacent names | MEDIUM | fix routine |
| S-041 | `users/views.py:848, 869, 971, 994, 1810, 2186, 2193` · `users/trainer_directory.py:152` | A trainer's rate is shown to a client as a bare number, in the TRAINER's currency and with no unit beside it — `_shown_coaching_price` converts nothing and the card carries no `coaching_currency` | The owner's rule (2026-09-21) is that every price a person sees is in their own currency. A pound client reading a dollar trainer's `100.00` will read it as pounds and be charged 1,300,000. The MONEY is right (`take_hold` converts); only the display is wrong. `GET /v1/coaching/status/` is already fixed; these eight are lists, so the fix is one batched wallet query per page, not a lookup per row (cf. S-026) | HIGH | currency |
| S-042 | `routine/views.py:3921, 3959, 3658, 3846, 3311` | Bare English in bodies beside the ones S-039 just translated: the success messages, "You do not have permission to modify this exercise", and bulk-complete's duplicate error (which also has no `code`) | An Arabic user reads English, and one of them cannot be branched on | MEDIUM | fix routine |
| S-043 | `social/views.py:489` `CommentViewSet` | No `perform_destroy`, while create increments `Post.comments_count` | Deleting a comment never decrements the badge; S-033 corrected for hidden and blocked rows, not deleted ones | MEDIUM | fix social |
| S-044 | `achievements/engine.py:301` | `challenge_wins` counts `status='completed'` and `rank=1` with no filter on the challenge having ENDED | The Challenge Winner badge fires the moment someone is temporarily first, and is never withdrawn when they drop | HIGH | fix social |
| S-045 | `home/sections.py:602` | `challenge_rank` excludes the viewer's own `dropped` row, but the `ahead` subquery counts dropped participants | A dropped rival still holds a place in front of them. Low impact now `leave` deletes the row; `dropped` is reachable only from the admin | MEDIUM | fix social |
| S-046 | `diet/views.py:2511` | Unreachable branch: `if target_date_str and target_date is None` cannot be true — the `bad_date` return above already covers it. The dead body is also untranslated and has no `code` | Dead code that reads like a live guard | MEDIUM | fix people |
| S-047 | `diet/views.py:2497, 2682, 2742` | The three "Only clients can access progress tracking" 403s carry no `code` | Same class as the just-closed S-035, in `diet` | MEDIUM | fix people |
| S-048 | `diet/views.py:1545` | `DailyAdviceView`'s 500 body has no `code`, where its siblings in the same file answer `server_error` | The app cannot branch on it | MEDIUM | fix people |
| S-049 | `subscription/models.py:217-223` | `Subscription.save()` re-inherits all five `has_*` flags from the plan on EVERY save, while the fields' own comment (L165) says they can be overridden | Any operator grant is silently reverted by the next save. **Filed by the money build, then lost to an id collision — re-filed** | HIGH | money |
| S-050 | `ai_assistant/consumers.py:333-338` | The AI daily-limit reset is computed with naive `datetime.combine`/`datetime.now()` under `USE_TZ` with `TIME_ZONE=Asia/Damascus` | The cache TTL is off by the UTC offset and `resets_at` reaches the client as a naive ISO string. **Lost to the same collision — re-filed** | MEDIUM | money |
| S-052 | `subscription/settings/gateway_config.py:67` | ShamCash `amount_limits` carries SYP only | Fail-closed by design, so a USD-charged checkout cannot clear the gateway at all until the owner supplies USD min/max | HIGH | currency |
| S-053 | `training_platform/file_security.py:136, 862` · `:130, 567` · `routine/models.py:962` ÷ `routine/views.py:1373` · and eight more pairs | The same sentence entered the catalogue twice, differing only by a full stop or a capital | Two Arabic entries to keep in step, free to drift apart. The policy pass lists every pair | MEDIUM | policy |
| S-054 | `tests/security/` (53 scripts) | Nothing collects them: `pytest.ini` matches `test_*.py`, and CI runs only four of them, only on push to `main`. `tests/security/README.md:30` still promises "Phase 14 converts them" | Fifty scripts that read as a security suite and are never run | MEDIUM | policy |

## Closed

| ID | Where | What | Closed by |
|---|---|---|---|
| S-001 | `wallet/utils.py:24` `get_platform_revenue_wallet` · `:43` `get_escrow_wallet` | The platform's revenue wallet is ONE wallet, so it holds one currency for ever — the first payment decides it. A plan priced in the other currency is minted into the client's wallet, logged as an error and never moves on. ESCROW has the same shape, and worse: a coaching month in the second currency could not even be held. | fix(currency): two currencies, both sold, with an operator-set rate between them |
| S-002 | `routine/serializers.py:463` | `completed_sets` and `target_sets` are writable on `UserExerciseProgressSerializer`; `RoutineProgressSerializer` locked exactly these as derived ("a client cannot author their own compliance") | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-003 | `routine/models.py:924` | `ExerciseSetLog.date` is independent of its progress row's `date` | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-004 | `routine/views.py:4025` | `client_progress` answers `client_info`; its own docstring promises `client` | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-005 | `routine/permissions.py:297` | `IsSessionOwnerOrTrainerOrAdmin.has_permission` refuses POST for non-trainers, but `WorkoutSessionViewSet.get_permissions` never uses that class for POST | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-006 | `routine/views.py:3460` | `ExerciseImageUploadView.post`'s broad handler returns `f'Upload failed: {str(e)}'` | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-007 | `routine/urls.py:19` | `ExerciseSetLogViewSet` is registered under TWO prefixes (`set-logs/`, `exercisesetlogs/`) | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-008 | `tests/security/*.py` (e.g. `privacy_rights.py:2`) | Every script hardcodes `sys.path.insert(0, '<the main checkout>')` | fix(side-bugs): close S-008 and S-011–S-015 |
| S-009 | `API_CONTRACT.md` §4 push table | Missing 10 registered event types (`coaching_*`, `personal_record`, `subscription_*`, `weekly_recap`, `trainer_weekly_digest`) | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-010 | `.gitignore:17` | `.venv/` with a trailing slash does not match the `.venv` SYMLINK a worktree gets | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-011 | `social/consumers.py:39` | `SocialConsumer.receive` echoes any frame back verbatim | fix(side-bugs): close S-008 and S-011–S-015 |
| S-012 | `ai_assistant/consumers.py:272` · `views.py:112` · `services/chat_service.py:222-252` | Error paths with no `code` and bare untranslated literals, beside others that have both | fix(side-bugs): close S-008 and S-011–S-015 |
| S-013 | `ai_assistant/consumers.py:224` | `_check_rate_limit` increments up front, so a turn that yields only an error still spends the daily allowance | fix(side-bugs): close S-008 and S-011–S-015 |
| S-014 | `social/serializers.py:59-72`, `social/views.py:311,337,525` | Follower/post/like counts are unfiltered and denormalised | fix(side-bugs): close S-008 and S-011–S-015 |
| S-015 | `users/views.py:2044,1824,1845` | Trainer-request refusals with no `code`, next to newer ones that have one | fix(side-bugs): close S-008 and S-011–S-015 |
| S-016 | `routine/serializers.py:1415, 1679` | `profile_picture` returned as a relative path where every other endpoint returns it absolute (B-032 was this exact bug) | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-017 | `routine/views.py:3258` | Exercise media `content` accepts any http(s) URL — no host allow-list | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-018 | `training_platform/file_security.py:59` | The quarantine directory is always `MEDIA_ROOT/quarantine`, local | fix(side-bugs): close S-018, S-019, S-024 and S-038 |
| S-019 | `policy/exceptions.py:11,17` · `policy/models.py:53,62,64,100` | Every user- and operator-facing string in the policy app goes through gettext but has NO Arabic entry | fix(side-bugs): close S-018, S-019, S-024 and S-038 |
| S-020 | `training_platform/middleware.py:597` | `response['Vary'] = '...'` ASSIGNS instead of calling `patch_vary_headers`, discarding any `Vary` already set (`Cookie` from the session middleware, `Accept-Encoding` from gzip) | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-021 | `social/views.py:903` `_update_challenge_rankings` | Ranks are written only from `update_progress`, with no tiebreaker, one full-row `save()` per participant | fix(side-bugs): close S-021, S-027, S-033 and S-034 |
| S-022 | `routine/models.py:833` (`WorkoutSession.status`) | Nothing ever writes `abandoned`: no sweep closes a session the client walked away from | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-023 | `diet/trainer_services.py:707,720` (`get_daily_progress`, `get_enhanced_daily_progress`) | A GET endpoint's service does `get_or_create` plus `SELECT … FOR UPDATE` plus `save()` on every read | fix(side-bugs): close S-023, S-025, S-026 and S-035 |
| S-024 | `subscription/views.py:370` `SubscriptionViewSet.current` | The 404 body is `{"message": …}` with no `code`, where §2 of the contract says the app branches on `code` | fix(side-bugs): close S-018, S-019, S-024 and S-038 |
| S-025 | `diet/views.py:1462` `DailyAdviceView` | The docstring says it requires AI-advice access; the code uses `HasDietAccess`, and nothing checks AI consent | fix(side-bugs): close S-023, S-025, S-026 and S-035 |
| S-026 | `users/trainer_directory.py:173` `_score` / `_reason` | Every candidate's reasons are rendered in BOTH languages (two `translation.override`s and a catalogue lookup each, up to seven per trainer) before the top three are sliced | fix(side-bugs): close S-023, S-025, S-026 and S-035 |
| S-027 | `social/views.py:562` | `interaction_actions` names a `leave` action the viewset does not define | fix(side-bugs): close S-021, S-027, S-033 and S-034 |
| S-028 | `training_platform/privacy/registry.py:356` | `audit_coverage()`'s app list omits `mediakit`, so a new mediakit model holding a user FK is silently exempt | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-029 | `routine/views.py:3123` · `routine/serializers.py:1376` | Two different classes both named `RoutineTemplateExerciseSerializer` (fields `exercise_details` vs `exercise`) | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-030 | `routine/views.py:985` | `RoutineExerciseViewSet` has no `filterset_fields`, so a client cannot fetch one day of a routine by `?routine=&day=` | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-031 | `routine/views.py:3394, 3735` | `ExerciseImageUploadView` and `ExerciseAddMediaView.get` hand-roll exercise dicts instead of using a serializer | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-032 | `routine/views.py:1618` · `WorkoutSessionViewSet` | `set-logs/my-progress/` and the session list return UNPAGINATED lists | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-033 | `social/serializers.py:191` | The corrected comment count still includes comments hidden by moderation; the denormalised column is never decremented on a hide | fix(side-bugs): close S-021, S-027, S-033 and S-034 |
| S-034 | `ai_assistant/services/chat_service.py:335-344` | `_iteration_status` returns five bare English strings sent to the client as `status` frames | fix(side-bugs): close S-021, S-027, S-033 and S-034 |
| S-035 | `users/views.py:2016-2028, 1740` | `request_id is required` and `action must be either "approve" or "reject"` still carry no `code` | fix(side-bugs): close S-023, S-025, S-026 and S-035 |
| S-036 | `routine/views.py:412` | `ExerciseCreateWithImageView.post`'s broad handler returns `f'Failed to create exercise: {str(e)}'` | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-037 | `routine/views.py:22` | `DetailedClientProgressSerializer` is imported and never used; its `get_client_info` is unreachable | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-038 | `tests/security/phase7_write.py:38` | Posts `completed_sets`/`target_sets` to `user-exercise-progress/`, which are now correctly ignored | fix(side-bugs): close S-018, S-019, S-024 and S-038 |
| S-039 | `routine/views.py:3684` | `add-media/`'s `errors[]` strings are untranslated English, beside the codes just added next to them | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-040 | `wallet/models.py:191` and `:474` | `Transaction.currency` defaulted to the literal `"USD"`, and `move_funds_atomic` fell back to the same literal — the exact hardcoding whose twin on `Wallet.currency` minted 5,000 SYP into a wallet labelled USD. Re-filed: the money build wrote it as S-020, the renumber moved it to S-037, and the second side-bug merge dropped the row. | fix(currency): two currencies, both sold, with an operator-set rate between them |
