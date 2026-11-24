"""Walk states: community (`users/walk/__init__.py` says how to add one).

Owned by the phase 3 worker: challenges, posts and comments, reports, follows.
"""
from . import state
from .framework import on_retire


@on_retire
def _hide_what_a_retired_walk_account_hosted(users):
    """A challenge outlives its creator's account in the lists (its creator is only
    served as null, `users/visibility.py`), so every run would add the old walk
    challenges to the community list. The operator's own switch takes them down:
    `is_hidden`, which hides a challenge from every list and stops every join. Walk data
    only — `users` are walk accounts being retired."""
    from social.challenge_cache import bump_everyone
    from social.models import Challenge

    hosted = list(Challenge.objects.filter(creator__in=users, is_hidden=False))
    for challenge in hosted:
        challenge.is_hidden = True
        challenge.save(update_fields=["is_hidden"])
    if hosted:
        bump_everyone()


@on_retire
def _close_the_reports_a_retired_walk_account_filed(users):
    """A report waits on the operator's desk (the console's queue, the operator phone's
    `community_reports` count) whoever filed it, so every run of `client_reports` would
    leave two more. Closed as an operator closes one, through `save()`: dismissed, with
    the reason in the resolution note."""
    from django.db.models import Q
    from django.utils import timezone

    from moderation.models import UNRESOLVED_STATUSES, Report, ReportStatus

    waiting = Report.objects.filter(status__in=UNRESOLVED_STATUSES).filter(
        Q(reporter__in=users) | Q(target_type="user", target_id__in=[u.pk for u in users]))
    for report in waiting:
        report.status = ReportStatus.DISMISSED
        report.handled_at = timezone.now()
        report.resolution_note = "Walk-through account reset (seed_walk_states)."
        report.save()


@state("member_with_post", area="community", role="client",
       summary="A client with a post of their own and a comment on it from another member.")
def member_with_post(walk):
    from social.models import Comment, Post

    me = walk.account()
    friend = walk.account("friend", first_name="رنا", last_name="حمدان", gender="Female")
    post = Post.objects.create(author=me, content="أنهيت أول أسبوع من برنامجي اليوم.")
    comment = Comment.objects.create(post=post, author=friend, content="بالتوفيق!")
    walk.ids(post=post.pk, comment=comment.pk, friend=friend.pk)


def _challenge(walk, creator, title, *, metric, days_back=3, days_on=14, target=50,
               unit="", **extra):
    """A challenge made through `POST /api/social/challenges/` (the host limit, the
    exercise and audience rules all apply), running from `days_back` days ago."""
    from datetime import timedelta

    from django.utils import timezone

    now = timezone.now()
    body = {"title": title, "description": "تحدٍّ للتجربة.", "challenge_type": "workout",
            "start_date": (now - timedelta(days=days_back)).isoformat(),
            "end_date": (now + timedelta(days=days_on)).isoformat(),
            "target_value": target, "unit": unit, "metric": metric, **extra}
    _status, made = walk.api(creator, "post", "/api/social/challenges/", body)
    return made["id"]


@state("client_challenges", area="community", role="client",
       summary="A client hosting three challenges (the limit), and in one challenge of "
               "each newer kind with progress: exercise reps, a streak, training days, "
               "and their coach's clients-only one.")
def client_challenges(walk):
    from datetime import timedelta

    from django.utils import timezone

    from .shared import coach_client, starter_exercise, sync_sets

    me = walk.account()
    coach = walk.account("coach", role="trainer", trainer_hourly_rate=0)
    coach_client(walk, me, coach)

    # Hosting three: the fourth answers 409 `challenge_host_limit`.
    hosted = [_challenge(walk, me, f"تحدي رقم {n}", metric="manual", target=10,
                         unit="جلسات") for n in (1, 2, 3)]

    # One of each newer kind: three by the coach (their own limit is three too), the
    # training-days one by another member.
    host = walk.account("host", first_name="جود", last_name="الحلبي", gender="Female")
    exercise = starter_exercise(walk)
    joined = {
        "exercise_reps": _challenge(walk, coach, "تكرارات الأسبوع", metric="exercise_reps",
                                    exercise=exercise.pk, target=300, unit="تكرار"),
        "streak_days": _challenge(walk, coach, "سلسلة الأيام", metric="streak_days",
                                  target=7, unit="أيام"),
        "training_days": _challenge(walk, host, "أيام التمرين", metric="training_days",
                                    target=12, unit="أيام"),
        "my_clients": _challenge(walk, coach, "تحدي متدربيّ", metric="sets",
                                 audience="my_clients", target=40, unit="مجموعات"),
    }
    # Three days of training (three sets a day, each a counted training day; a set
    # counts in a challenge when it arrives within LATE_HOURS of its day), then the joins,
    # each of which counts what is already logged inside the window.
    today = timezone.localdate()
    for back in (2, 1, 0):
        sync_sets(walk, me, exercise, today - timedelta(days=back), 3)
    for challenge_id in joined.values():
        walk.api(me, "post", f"/api/social/challenges/{challenge_id}/join/", {})
    walk.ids(coach=coach.pk, host=host.pk, hosted=hosted, exercise=exercise.pk,
             **{f"challenge_{kind}": pk for kind, pk in joined.items()})


@state("client_reports", area="community", role="client",
       summary="A client with a post and a comment of their own, and a report in each "
               "state: open, being reviewed, action taken, dismissed.")
def client_reports(walk):
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.middleware import SessionMiddleware
    from django.test import RequestFactory

    from admin_dashboard.admin import admin_site
    from moderation.models import Report, ReportStatus

    me = walk.account()
    other = walk.account("other", first_name="باسل", last_name="نعيم")
    operator = walk.account("operator", role="admin")

    _s, my_post = walk.api(me, "post", "/api/social/posts/",
                           {"content": "أول أسبوع في الصالة، والقادم أفضل."})
    _s, their_post = walk.api(other, "post", "/api/social/posts/",
                              {"content": "من يتمرّن صباحاً؟"})
    _s, my_comment = walk.api(me, "post", "/api/social/comments/",
                              {"post": their_post["id"], "content": "أنا! الساعة السابعة."})
    _s, their_comment = walk.api(other, "post", "/api/social/comments/",
                                 {"post": their_post["id"], "content": "رائع."})
    their_challenge = _challenge(walk, other, "تحدي باسل", metric="manual", target=5)

    def report(target_type, target_id, reason):
        _s, made = walk.api(me, "post", "/api/v1/reports/",
                            {"target_type": target_type, "target_id": target_id,
                             "reason": reason, "note": "للتجربة."})
        return made["id"]

    reports = {
        "open": report("social_post", their_post["id"], "spam"),
        "reviewing": report("comment", their_comment["id"], "harassment"),
        "actioned": report("user", other.pk, "impersonation"),
        "dismissed": report("challenge", their_challenge, "other"),
    }
    # The operator decides as the console decides: the change form's status for the
    # one being reviewed, the queue's two actions for the closed ones (both write the
    # operator, the time and an admin-log entry).
    reviewing = Report.objects.get(pk=reports["reviewing"])
    reviewing.status = ReportStatus.REVIEWING
    reviewing.handled_by = operator
    reviewing.save()
    report_admin = admin_site._registry[Report]
    for key, action in (("actioned", "mark_action_taken"), ("dismissed", "dismiss_reports")):
        request = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.user = operator
        request._messages = FallbackStorage(request)
        getattr(report_admin, action)(request, Report.objects.filter(pk=reports[key]))
    walk.ids(post=my_post["id"], comment=my_comment["id"], other=other.pk,
             operator=operator.pk, their_post=their_post["id"],
             **{f"report_{state_}": pk for state_, pk in reports.items()})
