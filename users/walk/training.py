"""Walk states: programmes and training (`users/walk/__init__.py` says how to add one).

Owned by the batch B worker. The database is a bootstrapped dev server's
(`framework.bootstrap_gaps`): the starter exercises and the platform's workouts are
there, and nothing here writes platform data. Everything a state does goes through
the app's own routes (`walk.api`) or, where there is no route, the service the route
or the console calls:

* money: the agent's top-up route (`shared.top_up`, at the state's ONE agent, in the
  client's own currency), the coaching request and its acceptance
  (`shared.coach_client`: month 1 held), the template's `generate/` that writes the
  client's programme and pays month 1 out, `users/coaching.py renew` for each later
  month (THIS relation only, never the sweep over every account), and the product's
  `buy/` route;
* the free first workout: `trial-workout/`;
* a workout older than the session routes take (30 days) is written as rows, as
  `seed_review_accounts` and `users/walk/progress.py` write them, with its activity
  dated to its own day so the badge engine judges the history once, at the end
  (`AchievementEngine.bulk_check_for_user`), by the days it happened.

A date that time makes is moved, and then the scheduled function is called.

What each state hands its agent in cash is declared beside it, and its `@needs` checks a
new agent's caps against those same constants (`framework.agent_cash_gaps`) before any
state writes anything: a cap below what a state moves refuses the run up front, naming
the state, the setting and the amount, instead of failing it part-way. No state here
buys or prices a subscription plan.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

from . import state
from .framework import agent_cash_gaps, needs

#: A coaching month's fee, in a new wallet's currency: the trainer's, which is the
#: client's (both are fresh accounts).
COACHING_FEE = Decimal("20000.00")

#: A product's price, in a new wallet's currency (the trainer's, which is the buyer's):
#: every product the walk trainers draft (`trainer.draft_product`).
PRODUCT_PRICE = Decimal("15000.00")

#: The trainer's week, from the starter catalogue: four workouts (push, pull, legs,
#: upper) and three rest days. (catalogue key, sets, reps)
WEEK = {
    1: [("barbell-bench-press", 4, 8), ("seated-dumbbell-shoulder-press", 3, 10),
        ("triceps-rope-pushdown", 3, 12)],
    2: [("lat-pulldown", 4, 10), ("barbell-bent-over-row", 4, 8), ("barbell-curl", 3, 12)],
    4: [("barbell-back-squat", 4, 8), ("barbell-romanian-deadlift", 3, 10),
        ("standing-calf-raise", 4, 15)],
    6: [("incline-barbell-bench-press", 4, 8), ("pull-up", 3, 8), ("crunch", 3, 20)],
}
WORKOUT_DAYS = sorted(WEEK)

#: A programme sold by the session: three sessions a week.
THREE_A_WEEK = {
    1: [("incline-dumbbell-press", 3, 10), ("hammer-curl", 3, 12)],
    2: [("leg-press", 3, 12), ("lying-leg-curl", 3, 12)],
    3: [("arnold-press", 3, 10), ("seated-cable-row", 3, 12)],
}


# ------------------------------------------------------------------ helpers
def the_operator(walk):
    """The state's operator (`walk.<state>.operator@`), made on first use: the console's
    decisions record who took them."""
    from users.models import CustomUser

    if "operator" in walk.accounts:
        return CustomUser.objects.get(pk=walk.accounts["operator"]["id"])
    return walk.account("operator", role="admin", is_staff=True)


def fund(walk, client, amount):
    """Cash for `client`'s wallet, taken by the state's ONE agent (made on first use)
    through the agent's own top-up route, in the client's currency (`shared.top_up`,
    which hands an amount above the per-top-up cap over in parts). What a state hands
    over is declared beside it, for its `@needs`."""
    from users.models import CustomUser

    from .shared import open_agent, top_up

    if "agent" in walk.accounts:
        agent = CustomUser.objects.get(pk=walk.accounts["agent"]["id"])
    else:
        agent = open_agent(walk)
    top_up(walk, agent, client, f"{Decimal(amount):.2f}")


def paid_coaching(walk, client, coach, *, months):
    """`coach` charges `COACHING_FEE` a month; `client` has `months` of it in the wallet,
    asks, and is accepted (`shared.coach_client`): month 1 is held. `months` is the
    state's declared constant, the one its `@needs` reads."""
    from .shared import coach_client

    if coach.trainer_hourly_rate and coach.trainer_hourly_rate != COACHING_FEE:
        # The agent's day is declared from COACHING_FEE: another fee would move more.
        raise ValueError(f"paid_coaching: the coach charges {coach.trainer_hourly_rate}, "
                         f"the states declare {COACHING_FEE}")
    if not coach.trainer_hourly_rate:
        coach.trainer_hourly_rate = COACHING_FEE
        coach.save(update_fields=["trainer_hourly_rate"])
    fund(walk, client, COACHING_FEE * months)
    return coach_client(walk, client, coach)


def coaching_since(relation, days_ago):
    """The coaching moved back `days_ago` days and renewed month by month, as the hourly
    run would have done: the dates move, then `users/coaching.py renew` (for this one
    relation) charges each month that has come due. The wallet must hold the months."""
    from users.coaching import renew

    shift = timedelta(days=days_ago)
    type(relation).objects.filter(pk=relation.pk).update(
        requested_at=relation.requested_at - shift,
        responded_at=relation.responded_at - shift)
    first = relation.periods.order_by("number").first()
    type(first).objects.filter(pk=first.pk).update(
        starts_at=first.starts_at - shift, ends_at=first.ends_at - shift)
    for _month in range(24):
        if renew(relation.pk) != "renewed":
            break
    relation.refresh_from_db()
    return relation


def template(walk, trainer, *, name, goal="strength", days=7, weeks=9, week=WEEK):
    """The trainer's template, through the template route (`POST templates/`), made of
    the starter catalogue's exercises. Answers its id."""
    from routine.models import Exercise

    keys = {key for rows in week.values() for key, _sets, _reps in rows}
    by_key = dict(Exercise.objects.filter(catalogue_key__in=keys)
                  .values_list("catalogue_key", "pk"))
    rows = [{"exercise_id": by_key[key], "day": day, "order": order, "sets": sets,
             "reps": reps, "rest_time": 90}
            for day, entries in week.items()
            for order, (key, sets, reps) in enumerate(entries, start=1)]
    _status, body = walk.api(trainer, "post", "/api/routine/templates/",
                             {"name": name, "goal": goal, "days": days, "weeks": weeks,
                              "exercises": rows})
    return body["id"]


def programme(walk, trainer, client, *, start, weeks=9, name="برنامج القوة · 9 أسابيع"):
    """A dated programme written for `client` through the template's `generate/`: the
    client's own copy, assigned, month 1 paid out. Answers the routine."""
    from routine.models import Routine

    source = template(walk, trainer, name=name, weeks=weeks)
    end = start + timedelta(days=weeks * 7 - 1)
    _status, body = walk.api(trainer, "post", f"/api/routine/templates/{source}/generate/",
                             {"client_id": client.pk, "start_date": start.isoformat(),
                              "end_date": end.isoformat(), "name": name})
    return Routine.objects.get(pk=body["id"])


def train(client, routine, day, on_date):
    """One finished workout of `routine`'s `day` on `on_date`, written as the phone wrote
    it on that day (`users/walk/progress.py`'s way): the session, its activity and its
    duration dated to the workout's end, each exercise's sets and the day's progress,
    then the day closed from the session (`record_day_quietly`). Badges are judged once,
    at the end of the state."""
    from django.utils import timezone

    from analytics.models import PerformanceMetric, UserActivity
    from routine.models import ExerciseSetLog, UserExerciseProgress, WorkoutSession
    from routine.outcomes import record_day_quietly

    start = timezone.make_aware(datetime.combine(on_date, time(18, 0)))
    end = start + timedelta(minutes=50)
    session = WorkoutSession.objects.create(
        user=client, routine=routine, day=day, start_time=start, end_time=end,
        status="completed", effort_rating=4)
    # What `on_workout_completed` writes, as it would have on the day; it then finds
    # this row (its once-per-session check) and writes nothing stamped today.
    [activity] = UserActivity.objects.bulk_create([UserActivity(
        user=client, activity_type="routine_completed", session_id=str(session.pk),
        user_agent="server", metadata={"routine_id": routine.pk})])
    UserActivity.objects.filter(pk=activity.pk).update(timestamp=end)
    [duration] = PerformanceMetric.objects.bulk_create([PerformanceMetric(
        user=client, metric_type="workout_duration", value=50.0, unit="min")])
    PerformanceMetric.objects.filter(pk=duration.pk).update(recorded_at=end)
    for row in routine.routine_exercises.filter(day=day).select_related("exercise"):
        sets = row.sets or 3
        progress, _created = UserExerciseProgress.objects.get_or_create(
            user=client, exercise=row.exercise, date=on_date, defaults={"target_sets": sets})
        logged = [ExerciseSetLog.objects.create(
            user_exercise_progress=progress, workout_session=session, set_number=number,
            weight=Decimal("40"), reps=row.reps or 10, rest_time=row.rest_time or 90,
            date=on_date) for number in range(1, sets + 1)]
        # Received as each set was done, not today.
        ExerciseSetLog.objects.filter(pk__in=[s.pk for s in logged]).update(
            created_at=start + timedelta(minutes=5))
        progress.completed_sets = sets
        progress.total_repetitions = sum(s.reps for s in logged)
        progress.total_weight = sum((s.weight or 0) * s.reps for s in logged)
        progress.save(update_fields=["completed_sets", "total_repetitions", "total_weight",
                                     "updated_at"])
    record_day_quietly(client, routine, day, on_date, via="session", session=session)
    return session


def judge_badges(user):
    """The badge engine over the whole history at once, by the days it happened."""
    from achievements.engine import AchievementEngine

    AchievementEngine.bulk_check_for_user(user)


def workout_days(days):
    """The routine days with work among 1..`days` (`WEEK` repeated)."""
    return [d for d in range(1, days + 1) if ((d - 1) % 7) + 1 in WORKOUT_DAYS]


# ------------------------------------------------------------------ 1. programmes
#: Months of the fee each coached state's client is handed in cash, at the state's one
#: agent, on one day (and held unsettled: no settlement happens in a state). Read by the
#: state and by its `@needs`.
FINISHED_MONTHS = 4     # month 1 held, months 2 and 3 renewed, one month to spare
TODAY_MONTHS = 1        # month 1, held


@needs("programme_finished")
def _what_programme_finished_takes():
    return agent_cash_gaps("programme_finished", in_a_day=COACHING_FEE * FINISHED_MONTHS)


@needs("programme_training_today")
def _what_programme_training_today_takes():
    return agent_cash_gaps("programme_training_today", in_a_day=COACHING_FEE * TODAY_MONTHS)


@state("programme_finished", area="training", role="client",
       summary="A trainer's 9-week programme that ended: week 9 of 9, `finished`, its "
               "missed days marked; the coaching still paid, month 3 running.")
def programme_finished(walk):
    from django.utils import timezone

    me = walk.account()
    trainer = walk.account("trainer", role="trainer")
    today = timezone.localdate()
    relation = paid_coaching(walk, me, trainer, months=FINISHED_MONTHS)
    start = today - timedelta(days=70)
    routine = programme(walk, trainer, me, start=start)
    relation = coaching_since(relation, 70)
    skipped = {9, 23, 44, 58}           # 58 in the last week: its slot reads `missed`
    for day in workout_days(routine.days):
        if day not in skipped:
            train(me, routine, day, start + timedelta(days=day - 1))
    judge_badges(me)
    walk.ids(trainer=trainer.pk, routine=routine.pk, relation=relation.pk)


@state("programme_training_today", area="training", role="client",
       summary="A trainer's 9-week programme two weeks in, every workout done on its "
               "date and today a training day (day 16, week 3 of 9).")
def programme_training_today(walk):
    from django.utils import timezone

    me = walk.account()
    trainer = walk.account("trainer", role="trainer")
    today = timezone.localdate()
    relation = paid_coaching(walk, me, trainer, months=TODAY_MONTHS)
    start = today - timedelta(days=15)
    routine = programme(walk, trainer, me, start=start)
    relation = coaching_since(relation, 15)
    # Days 1..15 on their own dates: the last workout is yesterday's (day 15), and day
    # 16, a workout, is today.
    for day in workout_days(15):
        train(me, routine, day, start + timedelta(days=day - 1))
    judge_badges(me)
    walk.ids(trainer=trainer.pk, routine=routine.pk, relation=relation.pk, today_day=16)


# ------------------------------------------------------------------ 2. a bought programme
#: The buyer is handed this many times PRODUCT_PRICE in cash, at the state's one agent,
#: on one day: the programme, with something left over. Read by the state and by its
#: `@needs`.
TIMES_THE_PRICE = 2


@needs("bought_programme_missed_day")
def _what_the_bought_programme_takes():
    return agent_cash_gaps("bought_programme_missed_day",
                           in_a_day=PRODUCT_PRICE * TIMES_THE_PRICE)


@state("bought_programme_missed_day", area="training", role="client",
       summary="A programme bought from a trainer (3 sessions a week, 4 weeks) 12 days "
               "in: four sessions done, days 7 to 4 ago missed, session 5 today.")
def bought_programme_missed_day(walk):
    import uuid

    from django.utils import timezone

    from routine.models import Routine

    from .trainer import product_on_sale

    me = walk.account()
    trainer = walk.account("trainer", role="trainer")
    product = product_on_sale(walk, trainer)
    if product.price != PRODUCT_PRICE:          # what its `@needs` priced the day from
        raise ValueError(f"bought_programme_missed_day: the product costs {product.price}, "
                         f"the state declares {PRODUCT_PRICE}")
    fund(walk, me, PRODUCT_PRICE * TIMES_THE_PRICE)
    walk.api(me, "post", f"/api/v1/products/{product.pk}/buy/",
             {"idempotency_key": f"walk-{uuid.uuid4().hex}"})
    routine = Routine.objects.get(purchase__product=product, purchase__buyer=me)
    today = timezone.localdate()
    # Bought 12 days ago: the copy's dates move back as the purchase day would have.
    Routine.objects.filter(pk=routine.pk).update(start_date=today - timedelta(days=12))
    routine.refresh_from_db()
    for session, days_ago in ((1, 12), (2, 10), (3, 8), (4, 3)):  # 7..4 days ago missed
        train(me, routine, session, today - timedelta(days=days_ago))
    judge_badges(me)
    walk.ids(trainer=trainer.pk, routine=routine.pk, product=product.pk,
             purchase=routine.purchase_id)


# ------------------------------------------------------------------ 5. new clients
@state("client_brand_new", area="training", role="client",
       summary="A client who has just signed up: no name, no birth date, no body or goal "
               "answers, no coach, no programme, nothing logged.")
def client_brand_new(walk):
    walk.account(onboarded=False)


@state("client_trial_only", area="training", role="client",
       summary="A client with no coach and no programme whose only history is the free "
               "first workout, done yesterday (at home).")
def client_trial_only(walk):
    from django.utils import timezone

    from routine.models import Routine

    me = walk.account(training_place="home")
    _status, answer = walk.api(me, "post", "/api/routine/trial-workout/", {})
    copy = Routine.objects.get(pk=answer["routine_id"])
    train(me, copy, 1, timezone.localdate() - timedelta(days=1))
    judge_badges(me)
    walk.ids(routine=copy.pk, template=answer["template_id"])
