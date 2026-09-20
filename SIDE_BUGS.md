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
