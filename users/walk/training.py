"""Walk states: programmes and training (`users/walk/__init__.py` says how to add one).

Owned by the batch B worker. Every state goes through the platform's real services:

* money only through the agent top-up the agent's route makes (`move_funds_atomic` with
  its `money_type`, `agents.post_topup`), the coaching approval that holds month 1
  (`users/views.py approve_trainer_relation`), `routine/assignment.py assign_routine`
  that pays it out, `users/coaching.py renew` for each later month (the function the
  hourly task calls, for THIS relation only: never the sweep over every account), and
  `routine/products.py buy`;
* a trainer's programme through `routine_from_template` and `assign_routine`;
* a day closed through `routine/outcomes.py record_day_quietly`, from a session.

Two things are written as rows, as `seed_review_accounts` writes them, because nothing
but the phone writes them: a trainer's own exercises and template (what the trainer's
editor saves), and a logged workout (the session, its sets and the day's progress,
`created_at` as the phone would have sent them). A date that time makes is moved, and
then the scheduled function is called.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

from . import state

#: A coaching month's fee in the trainer's currency (SYP, the platform's default).
COACHING_FEE = Decimal("20000.00")

#: The trainer's week: four workouts (push, pull, legs, upper) and three rest days.
WEEK = {
    1: [("ضغط صدر بالبار", "Upper Chest", 4, 8), ("ضغط كتف", "Front Deltoid", 3, 10),
        ("تمديد ترايسبس", "Triceps", 3, 12)],
    2: [("سحب علوي", "Lats", 4, 10), ("تجديف بالبار", "Upper Back", 4, 8),
        ("مرجحة بايسبس", "Biceps", 3, 12)],
    4: [("سكوات", "Front Quads", 4, 8), ("رفعة رومانية", "Hamstrings", 3, 10),
        ("رفع سمانة", "Calves", 4, 15)],
    6: [("ضغط مائل", "Upper Chest", 4, 8), ("عقلة", "Lats", 3, 8),
        ("كرنش", "Abdominals", 3, 20)],
}
WORKOUT_DAYS = sorted(WEEK)


# ------------------------------------------------------------------ helpers
def fund(walk, client, amount, *, agent=None):
    """An agent top-up of `client`'s wallet, as the agent's route writes it: the ledger
    row (`WALLET_MOVE`), the receivable the agent now owes, the client's notice. The
    state's agent is made on first use and suspended after each top-up, as
    `seed_review_accounts` leaves its agent: a walk password must mint nobody money."""
    from django.db import transaction

    from policy.money import MoneyType
    from users.models import CustomUser
    from wallet import agents
    from wallet.models import AgentProfile, ensure_agent_profile, move_funds_atomic, wallet_for

    if agent is None and "agent" in walk.accounts:
        agent = CustomUser.objects.get(pk=walk.accounts["agent"]["id"])
    if agent is None:
        agent = walk.account("agent", role="agent")
        ensure_agent_profile(agent)
    wallet = wallet_for(client, "client")
    left = Decimal(amount)
    with transaction.atomic():
        profile = agents.lock_profile(AgentProfile.objects.get(user=agent))
        if profile.status != "active":
            profile.status = "active"
            profile.save(update_fields=["status", "updated_at"])
        # As many top-ups as the agent's own per-transaction cap needs, as an agent at
        # the counter would make them.
        cap = getattr(profile, AgentProfile.PER_TRANSACTION_FIELDS[wallet.currency]) or left
        while left > 0:
            amount = min(left, cap)
            refusal = agents.usage_of(profile, wallet.currency).refusal(amount)
            if refusal is not None:
                raise RuntimeError(f"the walk agent cannot top up {amount}: {refusal.code}")
            tx = move_funds_atomic(None, wallet, amount, actor_id=agent.id, tx_type="topup",
                                   currency=wallet.currency,
                                   money_type=MoneyType.WALLET_MOVE,
                                   metadata={"agent_id": agent.id})
            agents.post_topup(profile, tx)
            agents.notify_client_on_commit(tx, client, profile)
            left -= amount
        profile.status = "suspended"
        profile.save(update_fields=["status", "updated_at"])
    return agent


def ask(trainer, client, *, note="أريد برنامجاً يناسب وقتي."):
    """The client asks the trainer: the pending relation the request route writes, the
    price quoted from the trainer's rate (`quote_agreed_price`)."""
    from django.utils import timezone

    from users.models import TrainerClientRelation
    from users.trainer_relations import quote_agreed_price

    relation = TrainerClientRelation(trainer=trainer, client=client, status="pending",
                                     initiated_by="client", requested_at=timezone.now(),
                                     client_note=note)
    quote_agreed_price(relation, trainer)
    relation.save()
    return relation


def coach(walk, trainer, client, *, months=1):
    """The client asks, with `months` months in the wallet, and the trainer accepts:
    month 1 is held in escrow (`approve_trainer_relation`)."""
    from users.views import approve_trainer_relation

    if trainer.trainer_hourly_rate is None:
        trainer.trainer_hourly_rate = COACHING_FEE
        trainer.save(update_fields=["trainer_hourly_rate"])
    fund(walk, client, trainer.trainer_hourly_rate * months)
    relation = ask(trainer, client)
    refusal, relation = approve_trainer_relation(relation, trainer)
    if refusal is not None:
        raise RuntimeError(f"the trainer could not accept: {refusal.data.get('code')}")
    return relation


def coaching_since(relation, days_ago):
    """Move the coaching back `days_ago` days and renew it month by month, as the hourly
    run would have: the dates move, then `users/coaching.py renew` (for this relation)
    charges each month that has come due. The wallet must hold the months."""
    from django.utils import timezone

    from users.coaching import renew

    shift = timedelta(days=days_ago)
    type(relation).objects.filter(pk=relation.pk).update(
        requested_at=relation.requested_at - shift,
        responded_at=(relation.responded_at or timezone.now()) - shift)
    first = relation.periods.order_by("number").first()
    type(first).objects.filter(pk=first.pk).update(
        starts_at=first.starts_at - shift, ends_at=first.ends_at - shift)
    for _month in range(24):
        if renew(relation.pk) != "renewed":
            break
    relation.refresh_from_db()
    return relation


def exercises(trainer, week=WEEK):
    """The trainer's own exercises for `week`, as the exercise editor saves them."""
    from routine.models import Exercise

    made = {}
    for rows in week.values():
        for name, muscle, _sets, _reps in rows:
            if name not in made:
                made[name] = (Exercise.objects.filter(created_by=trainer, name=name).first()
                              or Exercise.objects.create(
                                  name=name, name_ar=name, name_en=name, created_by=trainer,
                                  target_muscle=muscle, equipment="barbell"))
    return made


def template(trainer, *, name_ar, name_en, goal="strength", days=7, weeks=9, week=WEEK):
    """The trainer's template: `week`'s rows on its days, as the template editor saves
    them (`RoutineTemplateSerializer` writes these two models)."""
    from routine.models import RoutineTemplate, RoutineTemplateExercise

    library = exercises(trainer, week)
    template = RoutineTemplate.objects.create(
        name=name_en, name_en=name_en, name_ar=name_ar, goal=goal, days=days, weeks=weeks,
        created_by=trainer)
    for day, rows in week.items():
        for order, (name, _muscle, sets, reps) in enumerate(rows, start=1):
            RoutineTemplateExercise.objects.create(
                template=template, exercise=library[name], day=day, order=order,
                sets=sets, reps=reps, rest_time=90)
    return template


def programme(trainer, client, *, start, weeks=9, name_ar="برنامج القوة · 9 أسابيع",
              name_en="Strength · 9 weeks"):
    """A dated programme the trainer writes for the client (`templates/{id}/generate/`'s
    own calls: `routine_from_template`, then `assign_routine`, which pays month 1 out)."""
    from routine.assignment import assign_routine, routine_from_template

    source = template(trainer, name_ar=name_ar, name_en=name_en, weeks=weeks)
    days = weeks * 7
    routine = routine_from_template(source, created_by=trainer, days=days, start_date=start,
                                    end_date=start + timedelta(days=days - 1))
    assign_routine(routine, client, trainer)
    return routine


def train(client, routine, day, on_date, *, every_set=True):
    """One workout of `routine`'s `day` on `on_date`, logged as the phone logs it (a
    session, its sets, the day's progress), then the day closed from the session
    (`record_day_quietly`, `via="session"`). `every_set=False` leaves the last set of
    each exercise out: the day closes `short`."""
    from django.utils import timezone

    from routine.models import ExerciseSetLog, UserExerciseProgress, WorkoutSession
    from routine.outcomes import record_day_quietly

    start = timezone.make_aware(datetime.combine(on_date, time(18, 0)))
    session = WorkoutSession.objects.create(
        user=client, routine=routine, day=day, start_time=start,
        end_time=start + timedelta(minutes=50), status="completed", effort_rating=4)
    for row in routine.routine_exercises.filter(day=day).select_related("exercise"):
        sets = (row.sets or 3) - (0 if every_set else 1)
        progress, _created = UserExerciseProgress.objects.get_or_create(
            user=client, exercise=row.exercise, date=on_date,
            defaults={"target_sets": row.sets or 3})
        logged = [ExerciseSetLog.objects.create(
            user_exercise_progress=progress, workout_session=session, set_number=number,
            weight=Decimal("40"), reps=row.reps or 10, rest_time=row.rest_time or 90,
            date=on_date) for number in range(1, sets + 1)]
        ExerciseSetLog.objects.filter(pk__in=[s.pk for s in logged]).update(
            created_at=start + timedelta(minutes=5))
        progress.completed_sets = sets
        progress.total_repetitions = sum(s.reps for s in logged)
        progress.total_weight = sum((s.weight or 0) * s.reps for s in logged)
        progress.save(update_fields=["completed_sets", "total_repetitions", "total_weight",
                                     "updated_at"])
    record_day_quietly(client, routine, day, on_date, via="session", session=session)
    return session


def workout_days(days):
    """The routine days with work among 1..`days` (`WEEK` repeated)."""
    return [d for d in range(1, days + 1) if ((d - 1) % 7) + 1 in WORKOUT_DAYS]


# ------------------------------------------------------------------ states
@state("programme_finished", area="training", role="client",
       summary="A trainer's 9-week programme that ended: week 9 of 9, `finished`, its "
               "missed days marked; the coaching still paid.")
def programme_finished(walk):
    from django.utils import timezone

    me = walk.account()
    trainer = walk.account("trainer", role="trainer")
    today = timezone.localdate()
    relation = coach(walk, trainer, me, months=4)
    start = today - timedelta(days=70)
    routine = programme(trainer, me, start=start)
    relation = coaching_since(relation, 70)
    # Most of its 36 workouts, on their own dates; a few left undone.
    skipped = {9, 23, 44, 58}           # 58 in the last week: its slot reads `missed`
    for day in workout_days(routine.days):
        if day not in skipped:
            train(me, routine, day, start + timedelta(days=day - 1))
    walk.ids(trainer=trainer.pk, routine=routine.pk, relation=relation.pk)


@state("programme_training_today", area="training", role="client",
       summary="A trainer's 9-week programme two weeks in, every workout done so far "
               "and today a training day (week 3 of 9).")
def programme_training_today(walk):
    from django.utils import timezone

    me = walk.account()
    trainer = walk.account("trainer", role="trainer")
    today = timezone.localdate()
    relation = coach(walk, trainer, me)
    start = today - timedelta(days=15)
    routine = programme(trainer, me, start=start)
    relation = coaching_since(relation, 15)
    # Days 1..15 on their own dates: the last is yesterday, day 16 (a workout) is today.
    for day in workout_days(15):
        train(me, routine, day, start + timedelta(days=day - 1))
    walk.ids(trainer=trainer.pk, routine=routine.pk, relation=relation.pk, today_day=16)


@state("bought_programme_missed_day", area="training", role="client",
       summary="A programme bought from a trainer (3 sessions a week, 4 weeks) 12 days "
               "in: four sessions done, a missed day behind it, session 5 today.")
def bought_programme_missed_day(walk):
    from django.utils import timezone

    from routine.models import Routine
    from routine.products import buy

    me = walk.account()
    trainer = walk.account("trainer", role="trainer")
    product = product_on_sale(walk, trainer, three_a_week=True)
    fund(walk, me, product.price * 2)
    purchase, _created = buy(product.pk, me, idempotency_key=f"walk-{me.pk}")
    routine = Routine.objects.get(purchase=purchase)
    today = timezone.localdate()
    # Bought 12 days ago: the copy's dates move back, as the purchase day did.
    Routine.objects.filter(pk=routine.pk).update(start_date=today - timedelta(days=12))
    routine.refresh_from_db()
    for session, days_ago in ((1, 12), (2, 10), (3, 8), (4, 3)):  # 7..4 days ago missed
        train(me, routine, session, today - timedelta(days=days_ago))
    walk.ids(trainer=trainer.pk, routine=routine.pk, product=product.pk,
             purchase=purchase.pk)


THREE_A_WEEK = {
    1: [("ضغط صدر بالدمبل", "Middle Chest", 3, 10), ("تجديف بالدمبل", "Upper Back", 3, 10)],
    2: [("اندفاع أمامي", "Front Quads", 3, 12), ("جسر الحوض", "Glutes", 3, 12)],
    3: [("ضغط كتف بالدمبل", "Front Deltoid", 3, 10), ("بلانك", "Abdominals", 3, 30)],
}


def verified_trainer(walk, trainer):
    """The trainer verified through the verification flow: the documents uploaded, the
    application sent, an operator's approval (`trainer_verification/services.py`)."""
    from trainer_verification import services

    application = send_for_review(trainer)
    services.decide(application.pk, the_operator(walk), approve=True,
                    reason="Walk account: placeholder documents, development only.")
    trainer.refresh_from_db()
    return trainer


def send_for_review(trainer):
    """Upload every required document and send the application (pending review). The
    documents are `seed_review_accounts`'s placeholder images, which say they are not
    identity documents."""
    from django.core.files.base import ContentFile

    from trainer_verification import services, slots
    from users.management.commands.seed_review_accounts import _placeholder_document

    for index, slot in enumerate(slots.REQUIRED):
        services.upload_document(trainer, slot, ContentFile(
            _placeholder_document(slot, index), name=f"{slot}.png"))
    return services.submit(trainer)


def draft_product(trainer, source, *, title_ar, title_en, price="15000.00"):
    """A product draft, through the trainer's own write serializer (`POST
    /api/v1/trainer/products/`)."""
    from types import SimpleNamespace

    from routine.product_models import RoutineProduct
    from routine.product_serializers import TrainerProductWriteSerializer
    from wallet.models import currency_for

    serializer = TrainerProductWriteSerializer(
        data={"source_template_id": source.pk, "title_ar": title_ar, "title_en": title_en,
              "description_ar": "برنامج مكتوب بعناية لثلاثة أيام في الأسبوع.",
              "description_en": "A careful three-day-a-week programme.",
              "price": price, "preview_days": 1, "place": "gym"},
        context={"request": SimpleNamespace(user=trainer), "creating": True})
    serializer.is_valid(raise_exception=True)
    product = serializer.apply(RoutineProduct(trainer=trainer, currency=currency_for(trainer)))
    product.save()
    return product


def product_on_sale(walk, trainer, *, three_a_week=True, title_ar="ثلاثة أيام للقوة",
                    title_en="Three days of strength"):
    """A verified trainer's product, sent for review and approved by an operator: on
    sale (`routine/products.py submit`, `approve`)."""
    from routine import products

    if not trainer.trainer_is_verified:
        verified_trainer(walk, trainer)
    source = template(trainer, name_ar=title_ar, name_en=title_en, days=3, weeks=4,
                      week=THREE_A_WEEK)
    product = draft_product(trainer, source, title_ar=title_ar, title_en=title_en)
    products.submit(trainer, product.pk)
    products.approve(product.pk, the_operator(walk))
    product.refresh_from_db()
    return product


def the_operator(walk):
    """The state's operator (`walk.<state>.operator@`), made on first use."""
    from users.models import CustomUser

    if "operator" in walk.accounts:
        return CustomUser.objects.get(pk=walk.accounts["operator"]["id"])
    return walk.account("operator", role="admin", is_staff=True)
