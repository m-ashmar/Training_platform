"""Whose demonstration a viewer is shown for an exercise.

One rule, one function: `resolve_exercise_media`. Everything that puts an exercise's
media on the wire — the routine day, the exercise detail, the day checklist, the
standalone media route — asks this and nothing else. Two owners hold the same four
`exercise.*` slots (`mediakit/slots.py`), and deciding between them in more than one
place is how a client ends up watching their coach on one screen and a stock clip on
the next.

The rule never widens what a viewer may SEE. `routine.permissions.visible_exercises_for`
decides that, before anything here runs; an asset is exactly as visible as the exercise
it hangs on, and this module only chooses among assets on an exercise already allowed.
"""
from __future__ import annotations

from django.db.models import Q

from .models import MediaAsset
from .services import payload
from .slots import OwnerType

#: What `media_source` can say. The app branches on these, so they never change.
ROUTINE_AUTHOR = "routine_author"
MY_TRAINER = "my_trainer"
CATALOGUE = "catalogue"


def empty() -> dict:
    """The answer for an exercise with no media anywhere.

    A function, not a constant: every caller gets its own `media` dict, so a caller
    that fills one in cannot edit the answer every other exercise is holding.
    """
    return {"media": {}, "media_source": None, "media_trainer_id": None}


def connected_trainer_ids(viewer) -> set[int]:
    """The trainers whose private demonstrations this viewer is entitled to see.

    A client: every trainer they have an APPROVED relation with — the same record
    `routine.permissions` treats as the only source of truth, never the
    `assigned_trainer` display pointer. A trainer: themselves, so their own library
    beats the catalogue on their own screen. An admin: nobody in particular, so they
    see the catalogue (an admin is not a client of anyone, and guessing a coach for
    them would put one trainer's footage on an operator's screen at random).
    """
    if viewer is None or not getattr(viewer, "is_authenticated", False):
        return set()
    if getattr(viewer, "is_trainer", False):
        return {viewer.id}
    if getattr(viewer, "is_admin", False):
        return set()

    from users.models import TrainerClientRelation
    return set(TrainerClientRelation.objects
               .filter(client=viewer, status="approved")
               .values_list("trainer_id", flat=True))


def resolve_exercise_media(viewer, exercise_ids, *, routine_author_id=None,
                           request=None, connected=None) -> dict[int, dict]:
    """The media a viewer sees for each exercise. Batched: one query for the page.

    When a client opens an exercise, the media they see is, in order —
      a) the media the author of THAT routine put on that exercise (the coach who
         prescribed it is the one demonstrating it),
      b) failing that, the media of any trainer they have an approved relation with,
      c) failing that, the shared catalogue's own media.

    A client with no coach gets (c). A trainer looking at their own library sees their
    own over the catalogue — they are the trainer they are connected to, so (b) is
    themselves and `media_trainer_id` comes back as their own id.

    A WHOLE source wins, not a slot at a time. A coach who filmed `exercise.loop` and
    nothing else shows their loop and no poster, rather than their loop over the
    catalogue's still: the two were shot by different people in different places, and
    a page that mixes them looks like a mistake. `media_source` says which of the
    three lines answered, so the app can caption it.

    Returns `{exercise_id: {"media": {slot: payload}, "media_source": str | None,
    "media_trainer_id": int | None}}`, with an entry for every id asked for.
    `routine_author_id` is the `created_by` of the routine these exercises were read
    out of — omit it outside a routine, and line (a) simply does not apply.

    `connected` lets a caller that already knows the viewer's approved trainers pass
    them in, so a response resolving several routines pays for that query once.
    """
    ids = {int(i) for i in exercise_ids}
    out = {i: empty() for i in ids}
    if not ids:
        return out

    if connected is None:
        connected = connected_trainer_ids(viewer)
    # The routine's author is a candidate whether or not the viewer is their client:
    # they are the person who prescribed this work, and the client is reading their
    # routine. They are NOT given visibility of anything — the exercise was already
    # ruled visible before this ran.
    people = set(connected) | ({routine_author_id} if routine_author_id else set())

    # ONE query for the whole page: both owner types, every exercise, every candidate
    # trainer. `payload()` builds URLs from columns and opens no connection of its own.
    rows = MediaAsset.objects.filter(
        Q(owner_type=OwnerType.EXERCISE, owner_id__in=ids)
        | (Q(owner_type=OwnerType.TRAINER_EXERCISE, owner_id__in=ids,
             person_id__in=people) if people else Q(pk__in=[]))
    )

    catalogue: dict[int, dict] = {}
    by_person: dict[int, dict[int, dict]] = {}
    newest: dict[tuple[int, int], object] = {}
    for asset in rows:
        if asset.owner_type == OwnerType.EXERCISE:
            catalogue.setdefault(asset.owner_id, {})[asset.slot] = asset
        else:
            by_person.setdefault(asset.owner_id, {}).setdefault(asset.person_id, {})[
                asset.slot] = asset
            seen = newest.get((asset.owner_id, asset.person_id))
            if seen is None or asset.updated_at > seen:
                newest[(asset.owner_id, asset.person_id)] = asset.updated_at

    for exercise_id in ids:
        theirs = by_person.get(exercise_id, {})

        # (a) the author of this routine.
        if routine_author_id and theirs.get(routine_author_id):
            out[exercise_id] = _answer(theirs[routine_author_id], ROUTINE_AUTHOR,
                                       routine_author_id, request)
            continue

        # (b) a trainer this viewer is connected to. `users` allows a client only one
        # approved coach at a time (`unique_approved_trainer_per_client`), so this is
        # a set of one today — but "today" is not a guarantee, and a rule that picks
        # arbitrarily from a set would start showing a different coach between two
        # requests the day that changes. Most recently updated wins, lowest trainer
        # id breaks a tie. Decided from rows already in memory; no second query.
        mine = [pid for pid in theirs if pid in connected]
        if mine:
            pid = min(mine, key=lambda p: (-newest[(exercise_id, p)].timestamp(), p))
            out[exercise_id] = _answer(theirs[pid], MY_TRAINER, pid, request)
            continue

        # (c) the shared catalogue.
        shared = catalogue.get(exercise_id)
        if shared:
            out[exercise_id] = _answer(shared, CATALOGUE, None, request)

    return out


def _answer(assets: dict, source: str, trainer_id, request) -> dict:
    return {"media": {slot: payload(asset, request) for slot, asset in assets.items()},
            "media_source": source, "media_trainer_id": trainer_id}


class ExerciseMediaCache:
    """One response's worth of the rule, so a page of exercises costs one query.

    A nested `ExerciseSerializer` cannot see its siblings, so the view puts one of
    these in the serializer context (`routine.media_context.prepare`, reached through
    `ExerciseMediaMixin`) after naming every exercise on the page. `get()` then
    answers from memory.

    A call for an id nobody registered still answers CORRECTLY — it resolves that one
    exercise on its own — but it costs a query, which is the N+1 this class exists to
    prevent. A view that serialises exercises registers them; the query-count test in
    `tests/test_trainer_exercise_media.py` is what holds that.
    """

    def __init__(self, viewer, request=None):
        self._viewer = viewer
        self._request = request
        #: {routine author id or 0: exercise ids}. Grouped by author because line (a)
        #: of the rule is per routine: one pass per distinct author, so a day — one
        #: routine, one author — is one query however many exercises it holds.
        self._wanted: dict[int, set[int]] = {}
        self._resolved: dict[tuple[int, int], dict] = {}
        self._connected = None

    def want(self, exercise_ids, routine_author_id=None) -> None:
        """Register exercises to resolve. Call before anything is serialised."""
        ids = {int(i) for i in exercise_ids if i}
        if ids:
            self._wanted.setdefault(int(routine_author_id or 0), set()).update(ids)

    def get(self, exercise_id, routine_author_id=None) -> dict:
        if exercise_id is None:
            return empty()
        exercise_id, author = int(exercise_id), int(routine_author_id or 0)
        pending = self._wanted.pop(author, None)
        if pending:
            self._resolved.update(
                {(eid, author): answer for eid, answer
                 in self._resolve(pending, author or None).items()})
        hit = self._resolved.get((exercise_id, author))
        if hit is not None:
            return hit
        # Not registered: correct, but one query of its own. See the class docstring.
        answer = self._resolve([exercise_id], author or None).get(exercise_id) or empty()
        self._resolved[(exercise_id, author)] = answer
        return answer

    def _resolve(self, ids, author):
        if self._connected is None:
            self._connected = connected_trainer_ids(self._viewer)
        return resolve_exercise_media(self._viewer, ids, routine_author_id=author,
                                      request=self._request,
                                      connected=self._connected)
