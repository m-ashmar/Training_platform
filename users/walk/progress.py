"""Walk states: progress, badges and body measurements (`users/walk/__init__.py` says how
to add one).

Owned by the phase 3 worker.
"""
from . import state

#: Twelve weeks back to the first session, then about every ten days: nine finished
#: workouts, one short of the `workout_warrior` badge (10; `achievements/registry.py`).
SESSION_DAYS_BACK = (84, 74, 64, 54, 44, 34, 24, 14, 4)
#: Each circumference and the weight three times: twelve weeks ago, six, and today.
MEASUREMENTS = {"weight": (84.0, 82.0, 80.0), "waist": (92.0, 89.0, 87.0),
                "chest": (101.0, 102.0, 103.0), "arm": (34.0, 35.0, 35.5),
                "thigh": (58.0, 58.5, 59.0)}
MEASURED_DAYS_BACK = (84, 42, 0)


@state("client_progress", area="progress", role="client",
       summary="A client with twelve weeks of finished workouts (nine: one short of the "
               "next badge), three months of weight and body measurements, and a weight "
               "goal half done.")
def client_progress(walk):
    from datetime import datetime, time, timedelta

    from django.utils import timezone

    from analytics.models import PerformanceMetric, UserActivity
    from routine.assignment import assign_routine, routine_from_template
    from routine.models import RoutineTemplate

    from .shared import coach_client

    me = walk.account(weight=84)
    coach = walk.account("coach", role="trainer", trainer_hourly_rate=0)
    coach_client(walk, me, coach)

    today = timezone.localdate()
    template = RoutineTemplate.objects.get(platform_key="starter-build-muscle-gym")
    routine = routine_from_template(template, created_by=coach, days=template.days,
                                    start_date=today - timedelta(days=90),
                                    end_date=today + timedelta(days=30))
    assign_routine(routine, me, coach)
    exercise = routine.routine_exercises.select_related("exercise").order_by(
        "day", "order", "id").first().exercise

    # Twelve weeks of history, written as the app wrote each workout on its day — the
    # review accounts' way (`seed_review_accounts._history`): the session routes take a
    # workout at most 30 days late, so a past older than that has no route to come
    # through, and nothing but a phone logs a set.
    #
    # The workout's activity row is written ON ITS DAY. Left to the finish's receiver
    # (`analytics/signals.py`, after commit) it would be stamped NOW, and the badge
    # engine — run synchronously as each activity row is created — would judge the
    # history by today's clock: twice the workouts (a finished one today, a day with
    # sets on its day), and the night-owl and early-bird badges whenever the command
    # runs at night. So the row is written first, dated to the workout's end (a state
    # that time makes); the receiver finds it (its own once-per-session check) and
    # writes nothing; and the engine judges the whole history once, at the end, with
    # its own check (`AchievementEngine.bulk_check_for_user`).
    from routine.models import ExerciseSetLog, UserExerciseProgress, WorkoutSession
    from routine.outcomes import record_day_quietly

    sessions = []
    for number, back in enumerate(SESSION_DAYS_BACK):
        day = today - timedelta(days=back)
        routine_day = 1 + number % template.days
        start = timezone.make_aware(datetime.combine(day, time(18, 0)))
        end = start + timedelta(minutes=50)
        session = WorkoutSession.objects.create(
            user=me, routine=routine, day=routine_day, start_time=start, end_time=end,
            status="completed", effort_rating=4)
        # What `on_workout_completed` writes, as it would have on the day.
        [activity] = UserActivity.objects.bulk_create([UserActivity(
            user=me, activity_type="routine_completed", session_id=str(session.pk),
            user_agent="server", metadata={"routine_id": routine.pk})])
        UserActivity.objects.filter(pk=activity.pk).update(timestamp=end)
        [duration] = PerformanceMetric.objects.bulk_create([PerformanceMetric(
            user=me, metric_type="workout_duration", value=50.0, unit="min")])
        PerformanceMetric.objects.filter(pk=duration.pk).update(recorded_at=end)
        progress, _created = UserExerciseProgress.objects.get_or_create(
            user=me, exercise=exercise, date=day, defaults={"target_sets": 3})
        logged = [ExerciseSetLog.objects.create(
            user_exercise_progress=progress, workout_session=session, set_number=n,
            weight=40 + number, reps=10, rest_time=90, date=day) for n in (1, 2, 3)]
        # Received as each set was done, not today.
        ExerciseSetLog.objects.filter(pk__in=[row.pk for row in logged]).update(
            created_at=start + timedelta(minutes=5))
        progress.completed_sets = 3
        progress.total_repetitions = 30
        progress.total_weight = sum(row.weight * row.reps for row in logged)
        progress.save(update_fields=["completed_sets", "total_repetitions",
                                     "total_weight", "updated_at"])
        record_day_quietly(me, routine, routine_day, day, via="session", session=session)
        sessions.append(session.pk)

    # Weight and the four circumferences, three readings each, dated back.
    for metric, values in MEASUREMENTS.items():
        for value, back in zip(values, MEASURED_DAYS_BACK):
            _s, row = walk.api(me, "post", "/api/analytics/metrics/",
                               {"metric_type": metric, "value": value,
                                "unit": "kg" if metric == "weight" else "cm"})
            if back:
                PerformanceMetric.objects.filter(pk=row["id"]).update(
                    recorded_at=timezone.now() - timedelta(days=back))

    # A weight goal from 84 to 76 kg, now at 80: half done.
    _s, goal = walk.api(me, "post", "/api/analytics/goals/",
                        {"goal_type": "weight_loss", "title": "الوصول إلى ٧٦ كغ",
                         "start_value": 84, "target_value": 76, "unit": "kg",
                         "target_date": (today + timedelta(days=60)).isoformat()})
    walk.api(me, "post", f"/api/analytics/goals/{goal['id']}/update_progress/",
             {"new_value": 80})

    from achievements.engine import AchievementEngine

    AchievementEngine.bulk_check_for_user(me)
    walk.ids(coach=coach.pk, routine=routine.pk, sessions=sessions, goal=goal["id"],
             exercise=exercise.pk)
