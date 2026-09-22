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
| S-038 | `tests/security/phase7_write.py:38` | Posts `completed_sets`/`target_sets` to `user-exercise-progress/`, which are now correctly ignored | The script reads as proving a write is refused when nothing is being tested. Not pytest-collected | MEDIUM | fix round 2 |
<<<<<<< HEAD
| S-033 | `social/serializers.py:191` | The corrected comment count still includes comments hidden by moderation; the denormalised column is never decremented on a hide | The badge over-reports after a moderator hides a comment | MEDIUM | fix round 1 |
| S-034 | `ai_assistant/services/chat_service.py:335-344` | `_iteration_status` returns five bare English strings sent to the client as `status` frames | Same class as the S-012 defect just closed, but on the status path | MEDIUM | fix round 1 |
| S-019 | `policy/exceptions.py:11,17` · `policy/models.py:53,62,64,100` | Every user- and operator-facing string in the policy app goes through gettext but has NO Arabic entry | `app_channel_required` and `not_available_on_channel` reach the app in English only, against "Arabic is the primary language" | HIGH | theme |
| S-021 | `social/views.py:903` `_update_challenge_rankings` | Ranks are written only from `update_progress`, with no tiebreaker, one full-row `save()` per participant | `ChallengeParticipation.rank` is NULL for anyone who joined and never logged and stale for everyone else; two equal scores swap between runs — and the "Challenge Winner" badge keys on `rank=1` | HIGH | home |
=======
| S-039 | `routine/views.py:3684` | `add-media/`'s `errors[]` strings are untranslated English, beside the codes just added next to them | An Arabic user reads English in a 207 body | MEDIUM | fix round 2 |
| S-029 | `routine/views.py:3123` · `routine/serializers.py:1376` | Two different classes both named `RoutineTemplateExerciseSerializer` (fields `exercise_details` vs `exercise`) | The router uses one and `RoutineTemplateSerializer` the other; the next edit lands in the wrong one | MEDIUM | trainer media |
| S-030 | `routine/views.py:985` | `RoutineExerciseViewSet` has no `filterset_fields`, so a client cannot fetch one day of a routine by `?routine=&day=` | The day arrives only inside the whole-routine payload, though the template equivalent can be filtered | MEDIUM | trainer media |
| S-032 | `routine/views.py:1618` · `WorkoutSessionViewSet` | `set-logs/my-progress/` and the session list return UNPAGINATED lists | A long history is one unbounded response | MEDIUM | trainer media |
| S-035 | `users/views.py:2016-2028, 1740` | `request_id is required` and `action must be either "approve" or "reject"` still carry no `code` | Left from the S-015 pass, which closed the other twelve sites | MEDIUM | fix round 1 |
| S-019 | `policy/exceptions.py:11,17` · `policy/models.py:53,62,64,100` | Every user- and operator-facing string in the policy app goes through gettext but has NO Arabic entry | `app_channel_required` and `not_available_on_channel` reach the app in English only, against "Arabic is the primary language" | HIGH | theme |
| S-022 | `routine/models.py:833` (`WorkoutSession.status`) | Nothing ever writes `abandoned`: no sweep closes a session the client walked away from | A forgotten session stays `active` for ever, so every screen that asks "is a workout running" answers yes until someone edits the row. The home screen bounds it itself at 12 h (`home/sections.py LONGEST_SESSION`) rather than inherit it | MEDIUM | home |
<<<<<<< HEAD
>>>>>>> fix/social
| S-023 | `diet/trainer_services.py:707,720` (`get_daily_progress`, `get_enhanced_daily_progress`) | A GET endpoint's service does `get_or_create` plus `SELECT … FOR UPDATE` plus `save()` on every read | Three read routes write and take a row lock per page view, and two readers of the same day serialise. Home reads `DailyProgress` directly to avoid it | MEDIUM | home |
| S-024 | `subscription/views.py:370` `SubscriptionViewSet.current` | The 404 body is `{"message": …}` with no `code`, where §2 of the contract says the app branches on `code` | The app cannot tell "no subscription" from any other 404 | MEDIUM | home |
| S-025 | `diet/views.py:1462` `DailyAdviceView` | The docstring says it requires AI-advice access; the code uses `HasDietAccess`, and nothing checks AI consent | The guard the moderation build added is not on the endpoint that serves advice; home checks consent itself | MEDIUM | home |
| S-026 | `users/trainer_directory.py:173` `_score` / `_reason` | Every candidate's reasons are rendered in BOTH languages (two `translation.override`s and a catalogue lookup each, up to seven per trainer) before the top three are sliced | Wasted CPU per available trainer on `trainers/matches/` and on the first-day home — 0.1-0.5 ms each, so tens of ms at a few hundred trainers | MEDIUM | home |
=======
| S-024 | `subscription/views.py:370` `SubscriptionViewSet.current` | The 404 body is `{"message": …}` with no `code`, where §2 of the contract says the app branches on `code` | The app cannot tell "no subscription" from any other 404 | MEDIUM | home |
| S-027 | `social/views.py:562` | `interaction_actions` names a `leave` action the viewset does not define | Nobody can leave a challenge, and `participants_count` is only ever recomputed on join | MEDIUM | home |
>>>>>>> fix/people
| S-018 | `training_platform/file_security.py:59` | The quarantine directory is always `MEDIA_ROOT/quarantine`, local | With object storage the evidence of a rejected upload lands on ephemeral disk (the worker has no volume) and is gone at the next restart; also an `os.makedirs` per validator | MEDIUM | storage |

| S-001 | `wallet/utils.py:24` `get_platform_revenue_wallet` | The platform's revenue wallet is ONE wallet, so it holds one currency for ever — the first payment decides it. A plan priced in the other currency is minted into the client's wallet, logged as an error and never moves on. | Only SYP is sold today (USD is legacy rows), so nobody is affected yet; the day a second currency is priced, its revenue sits in customers' wallets. | MEDIUM | step 4 |

## Closed

| ID | Where | What | Closed by |
|---|---|---|---|
<<<<<<< HEAD
<<<<<<< HEAD
| S-022 | `routine/models.py:833` (`WorkoutSession.status`) | Nothing ever writes `abandoned`: no sweep closes a session the client walked away from | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-032 | `routine/views.py:1618` · `WorkoutSessionViewSet` | `set-logs/my-progress/` and the session list return UNPAGINATED lists | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-030 | `routine/views.py:985` | `RoutineExerciseViewSet` has no `filterset_fields`, so a client cannot fetch one day of a routine by `?routine=&day=` | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-007 | `routine/urls.py:19` | `ExerciseSetLogViewSet` is registered under TWO prefixes (`set-logs/`, `exercisesetlogs/`) | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-029 | `routine/views.py:3123` · `routine/serializers.py:1376` | Two different classes both named `RoutineTemplateExerciseSerializer` (fields `exercise_details` vs `exercise`) | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-043 | `social/views.py:489` `CommentViewSet` | No `perform_destroy`, while create increments `Post.comments_count` | Deleting a comment never decrements the badge; S-033 corrected for hidden and blocked rows, not deleted ones | MEDIUM | fix social |
| S-044 | `achievements/engine.py:301` | `challenge_wins` counts `status='completed'` and `rank=1` with no filter on the challenge having ENDED | The Challenge Winner badge fires the moment someone is temporarily first, and is never withdrawn when they drop | HIGH | fix social |
| S-045 | `home/sections.py:602` | `challenge_rank` excludes the viewer's own `dropped` row, but the `ahead` subquery counts dropped participants | A dropped rival still holds a place in front of them. Low impact now `leave` deletes the row; `dropped` is reachable only from the admin | MEDIUM | fix social |
| S-040 | `routine/views.py:22` | `WorkoutSessionDetailSerializer` and `ClientProfileViewSerializer` are imported and used nowhere | The same dead-import defect as the just-closed S-037, on the adjacent names | MEDIUM | fix routine |
| S-041 | `routine/views.py:3934` | `add-media/`'s DELETE `errors[]` carries no `code` at all — the POST got `errors_detail[]` in the S-017 pass, the DELETE did not | A 207 from a delete has nothing the app can branch on | MEDIUM | fix routine |
| S-042 | `routine/views.py:3921, 3959, 3658, 3846, 3311` | Bare English in bodies beside the ones S-039 just translated: the success messages, "You do not have permission to modify this exercise", and bulk-complete's duplicate error (which also has no `code`) | An Arabic user reads English, and one of them cannot be branched on | MEDIUM | fix routine |
| S-036 | `routine/views.py:412` | `ExerciseCreateWithImageView.post`'s broad handler returns `f'Failed to create exercise: {str(e)}'` | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-039 | `routine/views.py:3684` | `add-media/`'s `errors[]` strings are untranslated English, beside the codes just added next to them | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
| S-037 | `routine/views.py:22` | `DetailedClientProgressSerializer` is imported and never used; its `get_client_info` is unreachable | fix(routine): close S-007, S-022, S-029, S-030, S-032, S-036, S-037 and S-039 |
=======
| S-021 | `social/views.py:903` `_update_challenge_rankings` | Ranks are written only from `update_progress`, with no tiebreaker, one full-row `save()` per participant | fix(side-bugs): close S-021, S-027, S-033 and S-034 |
| S-027 | `social/views.py:562` | `interaction_actions` names a `leave` action the viewset does not define | fix(side-bugs): close S-021, S-027, S-033 and S-034 |
| S-033 | `social/serializers.py:191` | The corrected comment count still includes comments hidden by moderation; the denormalised column is never decremented on a hide | fix(side-bugs): close S-021, S-027, S-033 and S-034 |
| S-034 | `ai_assistant/services/chat_service.py:335-344` | `_iteration_status` returns five bare English strings sent to the client as `status` frames | fix(side-bugs): close S-021, S-027, S-033 and S-034 |
>>>>>>> fix/social
=======
| S-023 | `diet/trainer_services.py:707,720` (`get_daily_progress`, `get_enhanced_daily_progress`) | A GET endpoint's service does `get_or_create` plus `SELECT … FOR UPDATE` plus `save()` on every read | fix(side-bugs): close S-023, S-025, S-026 and S-035 |
| S-025 | `diet/views.py:1462` `DailyAdviceView` | The docstring says it requires AI-advice access; the code uses `HasDietAccess`, and nothing checks AI consent | fix(side-bugs): close S-023, S-025, S-026 and S-035 |
| S-026 | `users/trainer_directory.py:173` `_score` / `_reason` | Every candidate's reasons are rendered in BOTH languages (two `translation.override`s and a catalogue lookup each, up to seven per trainer) before the top three are sliced | fix(side-bugs): close S-023, S-025, S-026 and S-035 |
| S-035 | `users/views.py:2016-2028, 1740` | `request_id is required` and `action must be either "approve" or "reject"` still carry no `code` | fix(side-bugs): close S-023, S-025, S-026 and S-035 |
>>>>>>> fix/people
| S-008 | `tests/security/*.py` (e.g. `privacy_rights.py:2`) | Every script hardcodes `sys.path.insert(0, '<the main checkout>')` | fix(side-bugs): close S-008 and S-011–S-015 |
| S-011 | `social/consumers.py:39` | `SocialConsumer.receive` echoes any frame back verbatim | fix(side-bugs): close S-008 and S-011–S-015 |
| S-012 | `ai_assistant/consumers.py:272` · `views.py:112` · `services/chat_service.py:222-252` | Error paths with no `code` and bare untranslated literals, beside others that have both | fix(side-bugs): close S-008 and S-011–S-015 |
| S-013 | `ai_assistant/consumers.py:224` | `_check_rate_limit` increments up front, so a turn that yields only an error still spends the daily allowance | fix(side-bugs): close S-008 and S-011–S-015 |
| S-014 | `social/serializers.py:59-72`, `social/views.py:311,337,525` | Follower/post/like counts are unfiltered and denormalised | fix(side-bugs): close S-008 and S-011–S-015 |
| S-015 | `users/views.py:2044,1824,1845` | Trainer-request refusals with no `code`, next to newer ones that have one | fix(side-bugs): close S-008 and S-011–S-015 |
| S-002 | `routine/serializers.py:463` | `completed_sets` and `target_sets` are writable on `UserExerciseProgressSerializer`; `RoutineProgressSerializer` locked exactly these as derived ("a client cannot author their own compliance") | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-003 | `routine/models.py:924` | `ExerciseSetLog.date` is independent of its progress row's `date` | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-004 | `routine/views.py:4025` | `client_progress` answers `client_info`; its own docstring promises `client` | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-005 | `routine/permissions.py:297` | `IsSessionOwnerOrTrainerOrAdmin.has_permission` refuses POST for non-trainers, but `WorkoutSessionViewSet.get_permissions` never uses that class for POST | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-006 | `routine/views.py:3460` | `ExerciseImageUploadView.post`'s broad handler returns `f'Upload failed: {str(e)}'` | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-009 | `API_CONTRACT.md` §4 push table | Missing 10 registered event types (`coaching_*`, `personal_record`, `subscription_*`, `weekly_recap`, `trainer_weekly_digest`) | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-010 | `.gitignore:17` | `.venv/` with a trailing slash does not match the `.venv` SYMLINK a worktree gets | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-016 | `routine/serializers.py:1415, 1679` | `profile_picture` returned as a relative path where every other endpoint returns it absolute (B-032 was this exact bug) | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-017 | `routine/views.py:3258` | Exercise media `content` accepts any http(s) URL — no host allow-list | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-020 | `training_platform/middleware.py:597` | `response['Vary'] = '...'` ASSIGNS instead of calling `patch_vary_headers`, discarding any `Vary` already set (`Cookie` from the session middleware, `Accept-Encoding` from gzip) | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-028 | `training_platform/privacy/registry.py:356` | `audit_coverage()`'s app list omits `mediakit`, so a new mediakit model holding a user FK is silently exempt | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
| S-031 | `routine/views.py:3394, 3735` | `ExerciseImageUploadView` and `ExerciseAddMediaView.get` hand-roll exercise dicts instead of using a serializer | fix(side-bugs): close S-002–S-006, S-009, S-010, S-016, S-017, S-020, S-028 and S-031 |
