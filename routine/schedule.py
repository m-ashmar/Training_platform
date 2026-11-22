"""Which day of a programme is today, and the week around it — ONE rule, for every screen.

The owner's decision D15 (2026-11-22, Road to 10 S0.1, BACKEND_BUGS B-197): **today is the
next day not done, not the calendar cycle; a programme past its dates says it is
finished.** Home's hero named the day by the calendar cycle from `start_date`
(`(date - start) % days + 1`) and the week with no upper bound, while the app's Train tab
worked out "today" on the phone by another rule, so one account on one day read «week 12,
day 2» on Home and «week 9 of 9, day 1» on Train. `GET /api/routine/routines/{id}/schedule/`
answers this module, and the hero (`home/sections.py workout_hero`) reads the same
`Schedule`, so the two cannot disagree again.

THE DAYS. A routine is days 1..`days`; a day with at least one `RoutineExercise` row is a
WORKOUT day, the others are REST days. What a day number means differs by how the
routine was written — a calendar date (generated from a template with dates:
`days == end - start + 1`), a session (a buyer's copy of a routine product:
`template.days x template.weeks`, no end date), or a day of a cycle a trainer wrote by
hand — and the rule below holds for all three because it never maps a day to a date.

THE RULE.

* **Closures.** A day is closed by its `DayOutcome` row (`routine/outcomes.py`: every path
  that closes a day writes one). `complete` is `done`; anything else — sets missing, a
  skip, a day opened and swept as `unclosed` — is `short`. A short day is closed: it is
  never offered again as today (the app's rule since 2026-09-20; it still breaks the
  streak, which is `outcomes.py`'s business, not this one's).
* **Passes.** The closures are replayed in date order. A pass is the routine's workout days
  once each; when every one of them is closed, the next pass begins. A day closed twice in
  one pass counts once — a later COMPLETE close upgrades an earlier short one, nothing else
  changes it.
* **The next day not done** is the first workout day of the current pass that is not
  closed. A missed date does not move it: the workout waits (D15's reason: "a missed day
  should not make the plan skip a workout").
* **Rest days are kept, never owed.** After a closed day, the rest days that follow it in
  the routine (up to the next day not done) are taken one calendar day each: the day after
  a closure is the first of them, and so on. Once those days have passed, today is the
  next day not done, however many dates went by. Before anything is closed, the rest days
  that open the routine are counted from `start_date` the same way.
* **Today**, in this order: before `start_date` it is `not_started` (on the next day not
  done); a day closed TODAY is today's (`done` or `short`); a rest day owed today is
  `rest`; otherwise the next day not done, `today`. A finished programme has no today
  unless something was closed today.
* **Finished**: past `end_date`, or — for a routine that does not repeat — every workout
  day done. A routine does not repeat when its days cover its dates (`end_date` set and
  `days >= end - start + 1`) or when it has no end date and more than seven days (a
  programme written out in full: a buyer's copy, a generated multi-week programme). A
  routine of a week or less with no end date is a weekly cycle and runs on.

THE WEEK. `week` is the week of the programme counted from `start_date` (the same count
the trainer's queue uses for "week N of M", `users/attention_queue.py`), now bounded by
`weeks_total`: never week 12 of a 9-week programme. `weeks_total` comes from the dates when
both are set (`ceil((end - start + 1) / 7)`), else from a buyer's copy's template
(`weeks`), else `ceil(days / 7)` for a routine longer than a week, else null (a cycle with
no end). `slots` are the routine's own week holding today's day: the whole routine when it
has seven days or fewer, the template's `days` sessions for a buyer's copy, seven days
otherwise (TrainerWeek's weeks, `routine/weeks.py`); `slots_week` says which.

Reads: the routine's rows (one query), this person's outcomes on it (one query), and for a
buyer's copy its template's `days`/`weeks` (one more). Nothing is written.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta

DAYS_PER_WEEK = 7

#: The defaults the routine's own estimate assumes for a row with no number
#: (`RoutineSerializer.get_estimated_duration_minutes`): 3 sets, 10 reps, 60 s of rest.
EST_SETS, EST_REPS, EST_REST_SECONDS = 3, 10, 60
#: Seconds per rep, and between two exercises, in that same estimate.
EST_SECONDS_PER_REP = 4
EST_SECONDS_BETWEEN_EXERCISES = 120


def day_minutes(rows) -> int:
    """The estimated minutes of ONE day: the routine's own formula
    (`RoutineSerializer.get_estimated_duration_minutes`, which the app's `minutesOf`
    copies), applied to that day's rows only — 4 s a rep, the rests between sets, 2 min
    between exercises. 0 for a day with no rows.

    `rows` are dicts (or objects) with `sets`, `reps`, `rest_time`. BACKEND_BUGS B-196:
    the hero answered the whole routine's `estimated_duration` (45 or 60) on a card that
    describes one day, while Train and Day showed this number (28).
    """
    rows = list(rows)
    total = 0
    for row in rows:
        get = row.get if isinstance(row, dict) else (lambda key, r=row: getattr(r, key, None))
        sets = get('sets') or EST_SETS
        reps = get('reps') or EST_REPS
        rest = get('rest_time') or EST_REST_SECONDS
        total += sets * reps * EST_SECONDS_PER_REP
        if sets > 1:
            total += (sets - 1) * rest
    if len(rows) > 1:
        total += (len(rows) - 1) * EST_SECONDS_BETWEEN_EXERCISES
    return round(total / 60)


@dataclass
class Schedule:
    """One person's place in one routine on one date. `as_dict()` is the wire shape."""
    routine_id: int
    on_date: object
    days: int
    days_info: dict                     # {day: {'exercises', 'sets', 'estimated_minutes',
                                        #        'muscles', 'name'}} for workout days
    today: dict | None
    next_day: int | None
    week: int
    weeks_total: int | None
    finished: bool
    starts_on: object | None
    slots_week: int
    #: The day the slots are drawn around: today's, else the one the programme stopped on.
    focus_day: int = 1
    slots: list = field(default_factory=list)

    def counts(self, day) -> dict:
        """`{exercises, sets, estimated_minutes}` of one day (zeros on a rest day)."""
        info = self.days_info.get(day)
        if info is None:
            return {'exercises': 0, 'sets': 0, 'estimated_minutes': 0}
        return {'exercises': info['exercises'], 'sets': info['sets'],
                'estimated_minutes': info['estimated_minutes']}

    def muscles(self, day) -> list:
        info = self.days_info.get(day)
        return list(info['muscles']) if info else []

    @property
    def day(self) -> int:
        """The day the screens name: today's, else the one the programme stopped on."""
        if self.today is not None:
            return self.today['day']
        return self.focus_day

    def as_dict(self) -> dict:
        return {
            'routine_id': self.routine_id,
            'date': self.on_date.isoformat(),
            'today': self.today,
            'next_day': self.next_day,
            'week': self.week,
            'weeks_total': self.weeks_total,
            'finished': self.finished,
            'starts_on': self.starts_on.isoformat() if self.starts_on else None,
            'days_in_cycle': self.days,
            'slots_week': self.slots_week,
            'slots': self.slots,
        }


def _rows(routine) -> list:
    from .models import RoutineExercise

    return list(RoutineExercise.objects.filter(routine=routine)
                .order_by('day', 'order', 'id')
                .values('day', 'sets', 'reps', 'rest_time', 'exercise__target_muscle'))


def _days_info(routine, rows) -> dict:
    """Per workout day: its counts, its muscles (stored keys, in order, no duplicates —
    the app writes the words) and its name (`Routine.day_names`)."""
    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(row['day'], []).append(row)
    names = routine.day_names if isinstance(routine.day_names, dict) else {}
    info = {}
    for day, day_rows in grouped.items():
        muscles = []
        for row in day_rows:
            key = row['exercise__target_muscle']
            if key and key not in muscles:
                muscles.append(key)
        info[day] = {
            'exercises': len(day_rows),
            # The prescription's own count (`outcomes.day_plan`: a row with no set count
            # still asks for one), so a slot and the hero's `sets` are the same number.
            'sets': sum(max(row['sets'] or 0, 1) for row in day_rows),
            'estimated_minutes': day_minutes(day_rows),
            'muscles': muscles,
            'name': names.get(str(day)) or None,
        }
    return info


def _bought_template(routine):
    """`(days, weeks)` of the template a buyer's copy was written from, or (None, None)."""
    if getattr(routine, 'purchase_id', None) is None:
        return None, None
    from .product_models import RoutinePurchase

    found = (RoutinePurchase.objects.filter(pk=routine.purchase_id)
             .values_list('product__source_template__days',
                          'product__source_template__weeks').first())
    return found if found else (None, None)


def _replay(working, closures, on_date):
    """The closures, pass by pass. Returns `(passes, current, last, today_close)`:

    * `passes` — the completed passes, each `{day: (date, complete)}`;
    * `current` — the open pass, the same shape (never every workout day);
    * `last` — `(day, date, reset)` of the last closure that counted: `reset` is true
      when it completed a pass;
    * `today_close` — `(day, complete, pass_index)` of the last closure dated `on_date`,
      `pass_index` being the pass it belongs to (`len(passes)` = the open one).
    """
    wanted = set(working)
    passes, current, last, today_close = [], {}, None, None
    for day, date, complete in closures:
        if day not in wanted:
            continue
        complete = bool(complete)
        seen = current.get(day)
        if seen is not None:
            if complete and not seen[1]:
                # A short day done again in full: the slot is done now. The rest that
                # follows it starts from this date (the person trained today).
                current[day] = (date, True)
                if last is not None:
                    last = (last[0], max(last[1], date), last[2])
            if date == on_date:
                today_close = (day, complete or seen[1], len(passes))
            continue
        current[day] = (date, complete)
        index = len(passes)
        if len(current) == len(wanted):
            passes.append(current)
            current = {}
            last = (day, date, True)
        else:
            last = (day, date, False)
        if date == on_date:
            today_close = (day, complete, index)
    return passes, current, last, today_close


def _rest_owed(*, days, working, pointer, last, start_date, on_date):
    """The rest day `on_date` is, or None (see the module's rule)."""
    workout = set(working)

    def rest_between(low, high):
        return [day for day in range(low, high) if day not in workout]

    if last is None:
        if start_date is None:
            return None
        owed, anchor = rest_between(1, pointer), start_date - timedelta(days=1)
    else:
        day, anchor, reset = last
        if reset:
            owed = rest_between(day + 1, days + 1) + rest_between(1, pointer)
        elif pointer > day:
            owed = rest_between(day + 1, pointer)
        else:
            owed = []           # days done out of order: being caught up, none owed
    elapsed = (on_date - anchor).days
    if 1 <= elapsed <= len(owed):
        return owed[elapsed - 1]
    return None


def build(person, routine, on_date) -> Schedule:
    """The schedule of `person` on `routine` as of `on_date`."""
    from .models import DayOutcome

    days = max(routine.days or 1, 1)
    info = _days_info(routine, _rows(routine))
    working = sorted(day for day in info if 1 <= day <= days)
    closures = list(DayOutcome.objects
                    .filter(user=person, routine=routine, date__lte=on_date)
                    .order_by('date', 'id')
                    .values_list('day', 'date', 'complete'))
    passes, current, last, today_close = _replay(working, closures, on_date)
    pointer = next((day for day in working if day not in current), None)

    start, end = routine.start_date, routine.end_date
    span = (end - start).days + 1 if start and end and end >= start else None
    covers_dates = span is not None and days >= span
    repeats = not (covers_dates or (end is None and days > DAYS_PER_WEEK))
    finished = bool((end is not None and on_date > end) or (not repeats and passes))

    template_days, template_weeks = _bought_template(routine)
    if template_days:
        per_week = max(min(template_days, days), 1)
    elif days <= DAYS_PER_WEEK:
        per_week = days
    else:
        per_week = DAYS_PER_WEEK

    if span is not None:
        weeks_total = max(1, math.ceil(span / DAYS_PER_WEEK))
    elif template_weeks:
        weeks_total = template_weeks
    elif days > DAYS_PER_WEEK:
        weeks_total = math.ceil(days / per_week)
    else:
        weeks_total = None
    if start is None or on_date < start:
        week = 1
    else:
        week = (on_date - start).days // DAYS_PER_WEEK + 1
    if weeks_total is not None:
        week = min(week, weeks_total)

    def pass_at(index):
        return passes[index] if index < len(passes) else current

    today, shown, shown_day = None, current, pointer
    if today_close is not None:
        day, complete, index = today_close
        today = {'day': day, 'state': 'done' if complete else 'short'}
        shown, shown_day = pass_at(index), day
    elif finished:
        if not current and passes:
            shown, shown_day = passes[-1], last[0] if last else working[-1]
    elif start is not None and on_date < start:
        if pointer is not None:
            today = {'day': pointer, 'state': 'not_started'}
    elif pointer is not None:
        rest = _rest_owed(days=days, working=working, pointer=pointer, last=last,
                          start_date=start, on_date=on_date)
        if rest is not None:
            today = {'day': rest, 'state': 'rest'}
            shown_day = rest
            if last is not None and last[2] and rest > last[0]:
                shown = passes[-1]
        else:
            today = {'day': pointer, 'state': 'today'}
    if shown_day is None:
        shown_day = 1

    names = routine.day_names if isinstance(routine.day_names, dict) else {}
    block = (shown_day - 1) // per_week
    slots = []
    for day in range(block * per_week + 1, min((block + 1) * per_week, days) + 1):
        closed = shown.get(day)
        workday = info.get(day)
        if workday is None:
            state = 'rest'
        elif closed is not None:
            state = 'done' if closed[1] else 'short'
        elif today is not None and today == {'day': day, 'state': 'today'}:
            state = 'today'
        elif finished:
            state = 'missed'
        else:
            state = 'upcoming'
        counts = ({'exercises': workday['exercises'], 'sets': workday['sets'],
                   'estimated_minutes': workday['estimated_minutes']}
                  if workday else {'exercises': 0, 'sets': 0, 'estimated_minutes': 0})
        slots.append({
            'day': day,
            'name': names.get(str(day)) or None,
            'muscles': list(workday['muscles']) if workday else [],
            'state': state,
            'date': (start + timedelta(days=day - 1)).isoformat() if covers_dates else None,
            'closed_on': closed[0].isoformat() if closed is not None else None,
            'counts': counts,
        })

    return Schedule(
        routine_id=routine.pk, on_date=on_date, days=days, days_info=info,
        today=today, next_day=None if finished else pointer, week=week,
        weeks_total=weeks_total, finished=finished,
        starts_on=start if start is not None and on_date < start else None,
        slots_week=block + 1, focus_day=shown_day, slots=slots,
    )
