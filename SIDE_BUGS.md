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
| S-019 | `policy/exceptions.py:11,17` · `policy/models.py:53,62,64,100` | Every user- and operator-facing string in the policy app goes through gettext but has NO Arabic entry | `app_channel_required` and `not_available_on_channel` reach the app in English only, against "Arabic is the primary language" | HIGH | theme |
| S-020 | `training_platform/middleware.py:597` | `response['Vary'] = '...'` ASSIGNS instead of calling `patch_vary_headers`, discarding any `Vary` already set (`Cookie` from the session middleware, `Accept-Encoding` from gzip) | On exactly the four cached routes, a proxy may serve one user's variant to another | HIGH | theme |
| S-021 | `social/views.py:903` `_update_challenge_rankings` | Ranks are written only from `update_progress`, with no tiebreaker, one full-row `save()` per participant | `ChallengeParticipation.rank` is NULL for anyone who joined and never logged and stale for everyone else; two equal scores swap between runs — and the "Challenge Winner" badge keys on `rank=1` | HIGH | home |
| S-022 | `routine/models.py:833` (`WorkoutSession.status`) | Nothing ever writes `abandoned`: no sweep closes a session the client walked away from | A forgotten session stays `active` for ever, so every screen that asks "is a workout running" answers yes until someone edits the row. The home screen bounds it itself at 12 h (`home/sections.py LONGEST_SESSION`) rather than inherit it | MEDIUM | home |
| S-023 | `diet/trainer_services.py:707,720` (`get_daily_progress`, `get_enhanced_daily_progress`) | A GET endpoint's service does `get_or_create` plus `SELECT … FOR UPDATE` plus `save()` on every read | Three read routes write and take a row lock per page view, and two readers of the same day serialise. Home reads `DailyProgress` directly to avoid it | MEDIUM | home |
| S-024 | `subscription/views.py:370` `SubscriptionViewSet.current` | The 404 body is `{"message": …}` with no `code`, where §2 of the contract says the app branches on `code` | The app cannot tell "no subscription" from any other 404 | MEDIUM | home |
| S-025 | `diet/views.py:1462` `DailyAdviceView` | The docstring says it requires AI-advice access; the code uses `HasDietAccess`, and nothing checks AI consent | The guard the moderation build added is not on the endpoint that serves advice; home checks consent itself | MEDIUM | home |
| S-026 | `users/trainer_directory.py:173` `_score` / `_reason` | Every candidate's reasons are rendered in BOTH languages (two `translation.override`s and a catalogue lookup each, up to seven per trainer) before the top three are sliced | Wasted CPU per available trainer on `trainers/matches/` and on the first-day home — 0.1-0.5 ms each, so tens of ms at a few hundred trainers | MEDIUM | home |
| S-027 | `social/views.py:562` | `interaction_actions` names a `leave` action the viewset does not define | Nobody can leave a challenge, and `participants_count` is only ever recomputed on join | MEDIUM | home |
| S-008 | `tests/security/*.py` (e.g. `privacy_rights.py:2`) | Every script hardcodes `sys.path.insert(0, '<the main checkout>')` | Run from a worktree it silently exercises the MAIN tree, so a branch's own code is never the thing under test | HIGH | chat |
| S-009 | `API_CONTRACT.md` §4 push table | Missing 10 registered event types (`coaching_*`, `personal_record`, `subscription_*`, `weekly_recap`, `trainer_weekly_digest`) | The route table is gate-checked and this one is not, so it has drifted; the app session reads a short list | MEDIUM | chat |
| S-010 | `.gitignore:17` | `.venv/` with a trailing slash does not match the `.venv` SYMLINK a worktree gets | A `git add -A` in a worktree would commit it | MEDIUM | chat |
| S-011 | `social/consumers.py:39` | `SocialConsumer.receive` echoes any frame back verbatim | A live reflected-amplification surface; `ws_limits.py` notes it doubles the memory peak of a large frame | MEDIUM | chat |
| S-012 | `ai_assistant/consumers.py:272` · `views.py:112` · `services/chat_service.py:222-252` | Error paths with no `code` and bare untranslated literals, beside others that have both | The app cannot branch on them, and an Arabic user reads English | MEDIUM | moderation |
| S-013 | `ai_assistant/consumers.py:224` | `_check_rate_limit` increments up front, so a turn that yields only an error still spends the daily allowance | A user pays for the assistant's failures | MEDIUM | moderation |
| S-014 | `social/serializers.py:59-72`, `social/views.py:311,337,525` | Follower/post/like counts are unfiltered and denormalised | After a block the counts exceed the rows the viewer can see | MEDIUM | moderation |
| S-015 | `users/views.py:2044,1824,1845` | Trainer-request refusals with no `code`, next to newer ones that have one | The app branches on message text or not at all | MEDIUM | moderation |
| S-016 | `routine/serializers.py:1415, 1679` | `profile_picture` returned as a relative path where every other endpoint returns it absolute (B-032 was this exact bug) | The app gets two shapes for one field | MEDIUM | storage |
| S-017 | `routine/views.py:3258` | Exercise media `content` accepts any http(s) URL — no host allow-list | A coach can point every client's player at an arbitrary third-party host | HIGH | storage |
| S-018 | `training_platform/file_security.py:59` | The quarantine directory is always `MEDIA_ROOT/quarantine`, local | With object storage the evidence of a rejected upload lands on ephemeral disk (the worker has no volume) and is gone at the next restart; also an `os.makedirs` per validator | MEDIUM | storage |
| S-002 | `routine/serializers.py:463` | `completed_sets` and `target_sets` are writable on `UserExerciseProgressSerializer`; `RoutineProgressSerializer` locked exactly these as derived ("a client cannot author their own compliance") | A claim with no set log behind it completes a day. Staff-only today, so latent | HIGH | routine extras |
| S-003 | `routine/models.py:924` | `ExerciseSetLog.date` is independent of its progress row's `date` | `outcomes.day_work` counts a set by the LOG's date and `refresh_routine_progress` by the PROGRESS row's, so a mismatched date lands on two different days in two different figures | HIGH | routine extras |
| S-004 | `routine/views.py:4025` | `client_progress` answers `client_info`; its own docstring promises `client` | The app session writes the wrong model from the contract | MEDIUM | routine extras |
| S-005 | `routine/permissions.py:297` | `IsSessionOwnerOrTrainerOrAdmin.has_permission` refuses POST for non-trainers, but `WorkoutSessionViewSet.get_permissions` never uses that class for POST | Dead branch that reads like the rule — the next reader trusts it | MEDIUM | routine extras |
| S-006 | `routine/views.py:3460` | `ExerciseImageUploadView.post`'s broad handler returns `f'Upload failed: {str(e)}'` | Internal exception text reaches the client | MEDIUM | routine extras |
| S-007 | `routine/urls.py:19` | `ExerciseSetLogViewSet` is registered under TWO prefixes (`set-logs/`, `exercisesetlogs/`) | Every new action doubles the route surface and every contract row is written twice | MEDIUM | routine extras |
| S-001 | `wallet/utils.py:24` `get_platform_revenue_wallet` | The platform's revenue wallet is ONE wallet, so it holds one currency for ever — the first payment decides it. A plan priced in the other currency is minted into the client's wallet, logged as an error and never moves on. | Only SYP is sold today (USD is legacy rows), so nobody is affected yet; the day a second currency is priced, its revenue sits in customers' wallets. | MEDIUM | step 4 |

## Closed

| ID | Where | What | Closed by |
|---|---|---|---|
