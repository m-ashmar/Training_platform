"""Walk states: inbox (`users/walk/__init__.py` says how to add one).

Owned by the phase 3 worker.
"""
import re

from . import state

#: A value for every placeholder a notice template names, so each renders as it reads
#: in the app (`notifications/domain/*.py`; the resolver answers a fallback sentence for
#: a notice whose context lacks one).
SAMPLES = {
    "actor": "رنا", "name": "هادي", "points": 25, "title": "تحدي الأيام", "progress": 3,
    "unit": "أيام", "sender": "هادي", "preview": "كيف كان تمرين اليوم؟", "days": 3,
    "month": 2, "message": "أحسنت، استمر على هذا الإيقاع!", "exercise": "ضغط بالبار",
    "weight": 60, "reps": 8, "agent": "وكيل المزة", "number": "W-1001",
    "reason": "عنوان الاستلام غير صحيح", "trained": 3, "clients": 4, "attention": 1,
    "logged": 18, "planned": 24, "perfect": 2,
}

PLACEHOLDER = re.compile(r"%\((\w+)\)s")

#: The people ids a notice's `data` carries (API_CONTRACT §4), filled with the walk's own
#: two accounts; every other notice carries `type` alone (what it names is not made).
PEOPLE_KEYS = {
    "user_followed": ("follower_id",),
    "trainer_assignment_request": ("trainer_id",),
    "trainer_unassignment": ("trainer_id",),
    "client_request_received": ("client_id",),
    "client_request_approved": ("trainer_id", "client_id"),
    "client_request_rejected": ("trainer_id",),
    "client_request_cancelled": ("client_id",),
    "client_unassigned_trainer": ("client_id",),
    "coaching_ended": ("user_id", "client_id", "trainer_id"),
    "coaching_finished": ("user_id", "client_id", "trainer_id"),
}


def _context(template):
    names = set()
    for text in (str(template.title), str(template.body)) if template else ():
        names.update(PLACEHOLDER.findall(text))
    missing = names - set(SAMPLES)
    if missing:
        raise ValueError(f"inbox walk: no sample for {sorted(missing)}")
    return {name: SAMPLES[name] for name in names}


def kinds_for(role):
    """The notice kinds a role receives (`notifications/audience.py`), sorted."""
    from notifications.audience import AUDIENCE
    from notifications.channels.fcm import EVENT_CLASS_REGISTRY

    return sorted(kind for kind in EVENT_CLASS_REGISTRY if role in AUDIENCE.get(kind, ()))


@state("inbox_every_kind", area="inbox", role="client",
       summary="An inbox holding one notice of every kind a client receives, and the "
               "trainer part's of every kind a trainer receives (the three build-gated "
               "ones included).")
def inbox_every_kind(walk):
    import uuid

    from notifications.channels.fcm import _resolve_event_template
    from notifications.services import NotificationService

    me = walk.account()
    coach = walk.account("coach", role="trainer")
    people = {"client": (me, coach), "trainer": (coach, me)}
    written = {}
    for role, (recipient, counterpart) in people.items():
        for kind in kinds_for(role):
            # The ids a tap opens with, where they are people: the other one of the two.
            data = {"type": kind}
            for key in PEOPLE_KEYS.get(kind, ()):
                data[key] = (me.pk if key == "client_id" else
                             coach.pk if key == "trainer_id" else counterpart.pk)
            NotificationService.create_and_send(
                recipient=recipient, event_type=kind, related_object_id=uuid.uuid4().hex,
                metadata={"context": _context(_resolve_event_template(kind)),
                          "data": data})
            written.setdefault(role, []).append(kind)
    walk.ids(coach=coach.pk, client_kinds=written["client"],
             trainer_kinds=written["trainer"])
