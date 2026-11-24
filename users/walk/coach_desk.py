"""Walk states: a trainer's first steps and Today queue (`users/walk/__init__.py` says
how to add one). Kept apart from `trainer.py` (the batch B worker's).

Owned by the phase 3 worker.
"""
from . import state

#: The trainer's monthly fee in the queue state, in SYP: month 1 is held from it.
FEE = "10000.00"


@state("trainer_just_signed_up", area="coach_desk", role="trainer",
       summary="A trainer who has only signed up: no name, not verified, not accepting "
               "clients, no template, no client (the name step, first steps and the "
               "starter templates: F2.2, F1.11, F2.8).")
def trainer_just_signed_up(walk):
    # `onboarded=False`: the profile a sign-up leaves (no name, no birth date, no bio).
    walk.account(onboarded=False, trainer_is_available=False)
    walk.ids()


@state("trainer_full_queue", area="coach_desk", role="trainer",
       summary="A verified trainer whose Today queue holds an unanswered message, a "
               "client awaiting a first programme (month 1 held) and a request to accept, "
               "and whose roster shows a finished session to review.")
def trainer_full_queue(walk):
    from datetime import timedelta

    from django.utils import timezone

    from routine.assignment import assign_routine, routine_from_template
    from routine.models import RoutineTemplate, WorkoutSession
    from users.models import TrainerClientRelation

    from .shared import open_agent, sync_sets, top_up, verify_trainer

    me = walk.account(trainer_hourly_rate=FEE)
    operator = walk.account("operator", role="admin")
    verify_trainer(walk, me, operator)
    agent = open_agent(walk)

    def asks(part, **profile):
        client = walk.account(part, **profile)
        top_up(walk, agent, client, "30000.00")
        walk.api(client, "post", "/api/auth/client/request-trainer/", {"trainer_id": me.pk})
        return client, TrainerClientRelation.objects.get(trainer=me, client=client)

    def accept(relation):
        walk.api(me, "post", "/api/auth/trainer/respond-to-request/",
                 {"request_id": relation.pk, "action": "approve"})
        relation.refresh_from_db()

    # Coached, with a programme (which pays month 1 out), a workout finished today,
    # then a message the trainer has not answered.
    active, active_relation = asks("active", first_name="عمر", last_name="سليمان")
    accept(active_relation)
    template = RoutineTemplate.objects.get(platform_key="starter-build-muscle-gym")
    today = timezone.localdate()
    routine = routine_from_template(template, created_by=me, days=template.days,
                                    start_date=today - timedelta(days=7),
                                    end_date=today + timedelta(days=49))
    assign_routine(routine, active, me)
    started = timezone.now() - timedelta(minutes=55)
    _s, session = walk.api(active, "post", "/api/routine/workout-sessions/",
                           {"routine": routine.pk, "day": 1,
                            "start_time": started.isoformat()})
    exercise = routine.routine_exercises.select_related("exercise").order_by(
        "day", "order", "id").first().exercise
    sync_sets(walk, active, exercise, timezone.localdate(started), 3,
              session=WorkoutSession.objects.get(pk=session["id"]))
    walk.api(active, "patch", f"/api/routine/workout-sessions/{session['id']}/",
             {"status": "completed", "end_time": timezone.now().isoformat()})
    _s, thread = walk.api(active, "post", "/api/v1/chat/conversations/start/",
                          {"trainer_id": me.pk})
    conversation = thread.get("id") or thread.get("conversation", {}).get("id")
    _s, message = walk.api(active, "post",
                           f"/api/v1/chat/conversations/{conversation}/messages/",
                           {"body": "أنهيت تمرين اليوم، هل أزيد الوزن الأسبوع القادم؟"})

    # Accepted, no programme yet: month 1 is held.
    held, held_relation = asks("held", first_name="ديما", last_name="عساف", gender="Female")
    accept(held_relation)

    # Asked, not yet answered.
    asking, asking_relation = asks("asking", first_name="فادي", last_name="درويش")

    walk.ids(operator=operator.pk, agent=agent.pk, routine=routine.pk,
             active=active.pk, held=held.pk, asking=asking.pk,
             session=session["id"], conversation=conversation,
             message=message.get("id"), request=asking_relation.pk,
             held_relation=held_relation.pk)
