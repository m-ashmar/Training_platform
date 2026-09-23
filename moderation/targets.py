"""What a report can point at, who wrote it, and who is allowed to see it.

One table, `RESOLVERS`, with one entry per `TargetType`. Everything about a reportable
thing lives in its entry: the model it is a row of, the rule for whether a given person
may see it, and who authored it.

Two rules come out of this module and nothing else enforces them:

1. **You cannot report what you cannot see.** Without it, `POST reports/` is an oracle:
   send ids until one stops answering `target_not_found` and you have learned which
   private routines, diet plans and chat messages exist, and how many. Each type is
   checked with the SAME rule its own app reads it by — `visible_posts_for`,
   `visible_exercises_for` — so a change to visibility there cannot leave a stale copy
   here.
2. **You cannot report yourself.** Reporting your own post is noise in the queue;
   reporting your own account is a way to waste an operator's afternoon. Content the
   platform generated FOR a person (their diet plan, the AI's replies) has no author,
   so it stays reportable by the person it was generated for — that is exactly the
   complaint a store reviewer asks to be possible.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .models import TargetType


@dataclass(frozen=True)
class TargetSpec:
    """model: "app_label.ModelName" (for the console link).
    fetch: (viewer, target_id) -> the row when `viewer` may see it, else None.
    author: (row) -> the id of the person who WROTE it, or None when nobody did.
    """

    model: str
    fetch: Callable
    author: Callable


# --------------------------------------------------------------- the resolvers
# Each one is imported inside its function: `social.views` and `routine.permissions`
# both import this app's services, and pulling them in at module import time makes a
# cycle that fails at `manage.py check` rather than at run time.
def _fetch_user(viewer, target_id):
    from users.models import CustomUser

    # Any active account is reportable by any signed-in person: an abusive profile is
    # visible whether or not the two have ever interacted.
    return CustomUser.objects.filter(pk=target_id, is_active=True).first()


def _fetch_post(viewer, target_id):
    from social.views import visible_posts_for

    return visible_posts_for(viewer).filter(pk=target_id).first()


def _fetch_comment(viewer, target_id):
    from social.models import Comment
    from social.views import visible_posts_for

    return (Comment.objects.filter(pk=target_id, is_hidden=False,
                                   post__in=visible_posts_for(viewer))
            .select_related("post").first())


def _fetch_exercise(viewer, target_id):
    from routine.permissions import visible_exercises_for

    return visible_exercises_for(viewer).filter(pk=target_id).first()


def _fetch_routine(viewer, target_id):
    from django.db.models import Q

    from routine.models import Routine

    # The trainer who wrote it and the clients it was assigned to. A routine is one
    # client's private copy (`routine/assignment.py`), so this is a short list.
    return Routine.objects.filter(
        Q(created_by=viewer) | Q(assigned_to=viewer), pk=target_id).distinct().first()


def _fetch_diet_plan(viewer, target_id):
    from django.db.models import Q

    from diet.models import DietPlan

    # The client, and the trainer currently coaching them. `assigned_trainer` is the
    # approved relation's mirror on the client row (`users/models.py`), so a former
    # trainer loses the ability to report a plan along with everything else.
    return DietPlan.objects.filter(
        Q(user=viewer) | Q(user__assigned_trainer=viewer), pk=target_id).first()


def _fetch_chat_message(viewer, target_id):
    from ai_assistant.models import ChatMessage

    # Your own conversation only. Reporting the ASSISTANT's reply is the point of this
    # target type — a store reviewer asks for a way to flag what the AI said.
    return ChatMessage.objects.filter(
        pk=target_id, session__user=viewer).select_related("session").first()


def _fetch_coach_message(viewer, target_id):
    from django.db.models import Q

    from chat.models import Message

    # A message in a trainer–client conversation, reportable by the two people in it —
    # the same membership rule `chat/views.py` reads a thread by. A message its sender
    # took back is still a row in the reporter's thread (a tombstone), and harassment
    # that is deleted a minute later is exactly what needs reporting, so it stays
    # reportable; the operator reads the stored text.
    return (Message.objects
            .filter(Q(conversation__trainer=viewer) | Q(conversation__client=viewer),
                    pk=target_id)
            .first())


def _fetch_challenge(viewer, target_id):
    from social.models import Challenge

    # Every challenge the list serves is visible to every signed-in person
    # (`social.views.ChallengeViewSet`); one an operator has hidden is not, and so is
    # not reportable either.
    return Challenge.objects.filter(pk=target_id, is_hidden=False).first()


def _author_of_chat_message(row):
    # The person only wrote the messages with role "user"; everything else is the
    # model's output and has no author, so it stays reportable by the person it was
    # said to.
    return row.session.user_id if row.role == "user" else None


RESOLVERS: dict[str, TargetSpec] = {
    TargetType.USER: TargetSpec(
        model="users.CustomUser", fetch=_fetch_user, author=lambda row: row.pk),
    TargetType.SOCIAL_POST: TargetSpec(
        model="social.Post", fetch=_fetch_post, author=lambda row: row.author_id),
    TargetType.COMMENT: TargetSpec(
        model="social.Comment", fetch=_fetch_comment, author=lambda row: row.author_id),
    TargetType.EXERCISE: TargetSpec(
        model="routine.Exercise", fetch=_fetch_exercise,
        author=lambda row: row.created_by_id),
    TargetType.ROUTINE: TargetSpec(
        model="routine.Routine", fetch=_fetch_routine,
        author=lambda row: row.created_by_id),
    # Generated for the client by the planner, not written by anyone: no author, so
    # the client may report the plan they were given.
    TargetType.DIET_PLAN: TargetSpec(
        model="diet.DietPlan", fetch=_fetch_diet_plan, author=lambda row: None),
    TargetType.CHAT_MESSAGE: TargetSpec(
        model="ai_assistant.ChatMessage", fetch=_fetch_chat_message,
        author=_author_of_chat_message),
    TargetType.COACH_MESSAGE: TargetSpec(
        model="chat.Message", fetch=_fetch_coach_message,
        author=lambda row: row.sender_id),
    TargetType.CHALLENGE: TargetSpec(
        model="social.Challenge", fetch=_fetch_challenge,
        author=lambda row: row.creator_id),
}


def visible_target(target_type: str, target_id: int, viewer):
    """The row `viewer` is reporting, or None when they may not see it (or it is gone)."""
    spec = RESOLVERS.get(target_type)
    if spec is None:
        return None
    return spec.fetch(viewer, target_id)


def author_id_of(target_type: str, row) -> int | None:
    """Who wrote the row, or None for content the platform generated."""
    spec = RESOLVERS.get(target_type)
    return None if spec is None else spec.author(row)


def console_link(target_type: str, target_id: int, site_name: str = "admin") -> str | None:
    """The operator console's page for this target, so the queue can open it.

    `site_name` is the admin INSTANCE namespace (`AdminSite.name`), passed as
    `current_app`. Both admin sites in this project publish the same app namespace,
    `admin`, and without it every link from the operator console at `/dj-admin/`
    resolved to the stock site at `/admin/` — the operator would leave the console on
    every click.

    None when the type is unknown or the model is not registered on that site: a
    missing page must not take the whole queue down with a `NoReverseMatch`.
    """
    from django.urls import NoReverseMatch, reverse

    spec = RESOLVERS.get(target_type)
    if spec is None:
        return None
    app_label, model_name = spec.model.split(".")
    try:
        return reverse(f"admin:{app_label}_{model_name.lower()}_change",
                       args=[target_id], current_app=site_name)
    except NoReverseMatch:
        return None
