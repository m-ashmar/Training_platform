from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from routine.models import Routine, RoutineExercise, UserExerciseProgress, ExerciseSetLog, RoutineProgress
from django.utils import timezone
import random

class Command(BaseCommand):
    help = 'Mark full (but realistic) progress for user mmmm (mm@gmail.com) on all assigned routines.'

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.get(username='mmmm')
        except User.DoesNotExist:
            try:
                user = User.objects.get(email='mm@gmail.com')
            except User.DoesNotExist:
                self.stdout.write(self.style.ERROR("User 'mmmm' or 'mm@gmail.com' not found."))
                return

        routines = Routine.objects.filter(assigned_to=user)
        if not routines.exists():
            self.stdout.write(self.style.WARNING(f"No routines assigned to user {user.username} ({user.email})"))
            return

        today = timezone.now().date()
        for routine in routines:
            self.stdout.write(f"Processing routine: {routine.name}")
            for day in range(1, routine.days + 1):
                day_exercises = RoutineExercise.objects.filter(routine=routine, day=day)
                for rex in day_exercises:
                    # Mark UserExerciseProgress
                    progress, _ = UserExerciseProgress.objects.get_or_create(
                        user=user,
                        exercise=rex.exercise,
                        date=today,
                        defaults={
                            'completed_sets': rex.sets,
                            'target_sets': rex.sets,
                            'skipped': False,
                            'total_weight': 0,
                            'total_repetitions': 0,
                        }
                    )
                    total_reps = 0
                    total_weight = 0
                    for set_num in range(1, rex.sets + 1):
                        reps = random.choice([6, 7])
                        weight = rex.weight if rex.weight else random.uniform(20, 40)
                        set_log, _ = ExerciseSetLog.objects.get_or_create(
                            user_exercise_progress=progress,
                            set_number=set_num,
                            defaults={
                                'weight': weight,
                                'reps': reps,
                                'date': today,
                            }
                        )
                        total_reps += reps
                        total_weight += weight
                    progress.total_repetitions = total_reps
                    progress.total_weight = total_weight
                    progress.completed_sets = rex.sets
                    progress.target_sets = rex.sets
                    progress.save()
                # Mark RoutineProgress for the day as completed
                RoutineProgress.objects.update_or_create(
                    user=user,
                    routine=routine,
                    day=day,
                    defaults={
                        'status': 'Completed',
                        'exercises_completed': day_exercises.count(),
                        'total_exercises': day_exercises.count(),
                        'completion_time': timezone.timedelta(minutes=random.randint(30, 90)),
                        'notes': 'Auto-marked as completed by management command.'
                    }
                )
            self.stdout.write(self.style.SUCCESS(f"Marked full progress for routine: {routine.name}"))
        self.stdout.write(self.style.SUCCESS(f"All progress marked for user {user.username} ({user.email})!")) 