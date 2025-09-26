from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from routine.models import Routine, RoutineExercise, UserExerciseProgress, ExerciseSetLog, RoutineProgress, Exercise
from users.models import TrainerClientRelation
from django.utils import timezone
from datetime import timedelta

class Command(BaseCommand):
    help = 'Assigns a 7-day routine (3 sets per exercise) to user mmmm from trainer bdfb, and marks full progress with decreasing reps.'

    def handle(self, *args, **options):
        User = get_user_model()
        # 1. Get users
        try:
            trainer = User.objects.get(username='bdfb')
        except User.DoesNotExist:
            try:
                trainer = User.objects.get(email='ll@gmail.com')
            except User.DoesNotExist:
                self.stdout.write(self.style.ERROR("Trainer 'bdfb' or 'll@gmail.com' not found."))
                return
        try:
            client = User.objects.get(username='mmmm')
        except User.DoesNotExist:
            try:
                client = User.objects.get(email='mm@gmail.com')
            except User.DoesNotExist:
                self.stdout.write(self.style.ERROR("Client 'mmmm' or 'mm@gmail.com' not found."))
                return

        # 2. Ensure approved TrainerClientRelation
        relation, created = TrainerClientRelation.objects.get_or_create(
            trainer=trainer, client=client,
            defaults={'status': 'approved'}
        )
        if relation.status != 'approved':
            relation.status = 'approved'
            relation.save()

        # 3. Create 5 unique exercises if not exist
        exercises = []
        for i in range(1, 6):
            ex, _ = Exercise.objects.get_or_create(
                name=f"3SetRoutineEx{i}",
                defaults={
                    'description': f"Auto-generated 3-set exercise {i}",
                    'target_muscle': 'Other',
                    'difficulty_level': 'beginner',
                    'created_by': trainer,
                    'is_global': False,
                }
            )
            exercises.append(ex)

        # 4. Create a 7-day routine (1 week)
        routine_name = "1-Week 3-Set Routine"
        routine, _ = Routine.objects.get_or_create(
            name=routine_name,
            created_by=trainer,
            defaults={
                'description': 'Auto-generated 3-set routine for 1 week',
                'days': 7,
                'start_date': timezone.now().date(),
                'end_date': timezone.now().date() + timedelta(days=6),
                'is_active': True,
                'difficulty_level': 'beginner',
                'estimated_duration': 45,
            }
        )
        routine.assigned_to.add(client)
        routine.exercises.set(exercises)
        routine.save()

        # 5. Assign 5 exercises per day, same for each day
        RoutineExercise.objects.filter(routine=routine).delete()  # Clean up if rerun
        for day in range(1, 8):
            for order, ex in enumerate(exercises, 1):
                RoutineExercise.objects.create(
                    routine=routine,
                    exercise=ex,
                    sets=3,
                    reps=10,
                    rest_time=90,
                    day=day,
                    order=order,
                    weight=30 + 2*order,  # Example: 32, 34, 36, 38, 40
                    notes=f"Day {day} - Exercise {order}"
                )

        # 6. Mark progress for each day/exercise/set
        today = timezone.now().date()
        for day in range(1, 8):
            day_exercises = RoutineExercise.objects.filter(routine=routine, day=day)
            for rex in day_exercises:
                # Mark UserExerciseProgress
                progress, _ = UserExerciseProgress.objects.get_or_create(
                    user=client,
                    exercise=rex.exercise,
                    date=today + timedelta(days=day-1),
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
                for set_num in range(1, 4):
                    if set_num == 1:
                        reps = 10
                    elif set_num == 2:
                        reps = 8
                    else:
                        reps = 6
                    weight = rex.weight
                    set_log, _ = ExerciseSetLog.objects.get_or_create(
                        user_exercise_progress=progress,
                        set_number=set_num,
                        defaults={
                            'weight': weight,
                            'reps': reps,
                            'date': today + timedelta(days=day-1),
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
                user=client,
                routine=routine,
                day=day,
                defaults={
                    'status': 'Completed',
                    'exercises_completed': day_exercises.count(),
                    'total_exercises': day_exercises.count(),
                    'completion_time': timedelta(minutes=40),
                    'notes': 'Auto-marked as completed by management command.'
                }
            )
        self.stdout.write(self.style.SUCCESS(f"Assigned and marked full 3-set progress for user {client.username} on routine '{routine.name}' for 7 days!")) 