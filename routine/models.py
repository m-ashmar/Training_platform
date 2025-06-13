from django.db import models
from django.conf import settings
from django.utils.timezone import now
from django.utils import timezone


class Exercise(models.Model):
    name = models.CharField(max_length=255)  # Exercise name
    description = models.TextField(blank=True, null=True)  # Description of the exercise

    def __str__(self):
        return self.name


class ExerciseMedia(models.Model):
    exercise = models.ForeignKey(Exercise, on_delete=models.CASCADE, related_name='media')
    media_type = models.CharField(
        max_length=10,
        choices=[('video', 'Video'), ('photo', 'Photo'), ('text', 'Text')]
    )
    content = models.TextField()  # URL for video/photo or text content

    def __str__(self):
        return f"{self.media_type.capitalize()} for {self.exercise.name}"


class Routine(models.Model):
    name = models.CharField(max_length=255)  # Routine name
    description = models.TextField(blank=True, null=True)  # Optional description
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='created_routines')
    assigned_to = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name='assigned_routines')  
    is_active = models.BooleanField(default=True)  #
    days = models.PositiveIntegerField(default=3)  # Number of days in the routine plan

    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(auto_now=True)
    scheduled_date = models.DateTimeField(blank=True, null=True)  # Optional scheduling

    exercises = models.ManyToManyField(Exercise, blank=True)  
    start_date = models.DateField(default=now)  #
    
    end_date = models.DateField(blank=True, null=True)  # End date for the routine
    def get_assigned_users(self):
        """Return a comma-separated list of usernames assigned to this routine."""
        return ", ".join(user.username for user in self.assigned_to.all())

    get_assigned_users.short_description = 'Assigned Users'  # Display name in admin panel

    def __str__(self):
        return self.name
    

    def __str__(self):
        user_names = ', '.join(self.assigned_to.values_list('username', flat=True)[:5])  # Limit to 5 users
        extra = '' if self.assigned_to.count() <= 5 else '...'
        return f"{self.name} for {user_names}{extra}"

    def save(self, *args, **kwargs):
        """Override save to initialize progress for each user and day."""
        is_new = self.pk is None  # Check if it's a new instance
        super().save(*args, **kwargs)
        if is_new:
            # Initialize progress for all users
            for user in self.assigned_to.all():
                for day in range(1, self.days + 1):  # Create progress for all days baboshka
                    RoutineProgress.objects.create(user=user, routine=self, day=day)

    def update_progress(self, user, day, status):
        """Update or create progress for the user and specific day."""
        if day < 1 or day > self.days:
            raise ValueError(f"Day {day} is out of range for this routine.")
        progress_entry, created = RoutineProgress.objects.update_or_create(
            user=user,
            routine=self,
            defaults={'day': day, 'status': status}
        )
        return {
            "day": progress_entry.day,
            "status": progress_entry.status,
            "created": created  # True if new, False if updated
        }


class RoutineExercise(models.Model):
    routine = models.ForeignKey(Routine, on_delete=models.CASCADE, related_name='routine_exercises')
    exercise = models.ForeignKey(Exercise, on_delete=models.CASCADE, related_name='routine_exercises')
    sets = models.PositiveIntegerField(default=3)  # Number of sets
    repetitions = models.PositiveIntegerField(default=10)  # Repetitions per set
    rest_time = models.DurationField(default=timezone.timedelta(minutes=1))  # Rest time between sets

    day = models.PositiveIntegerField(default=1)  # Specifies which day the exercise belongs to
    order = models.PositiveIntegerField(default=1)  # Order of the exercise in the routine

    def __str__(self):
        return f"{self.exercise.name} in {self.routine.name} (Order: {self.order})"


class UserExerciseProgress(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='exercise_progress')
    exercise = models.ForeignKey(Exercise, on_delete=models.CASCADE, related_name='progress')
    date = models.DateField(default=now)  # Date the progress is recorded
    completed_sets = models.PositiveIntegerField(default=0)  # Number of sets completed
    target_sets = models.PositiveIntegerField(default=0)  # Total sets required for the exercise
    skipped = models.BooleanField(default=False)  # Whether the exercise was skipped

    def __str__(self):
        return f"{self.user.username} - {self.exercise.name} on {self.date}"

    def calculate_training_volume(self):
        """
        Calculate total training volume based on set logs.
        """
        total_volume = sum(
            log.weight for log in self.set_logs.all()
        )
        return total_volume


class RoutineProgress(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='routine_progress')
    routine = models.ForeignKey(Routine, on_delete=models.CASCADE, related_name='progress')
    day = models.PositiveIntegerField()  # Day of the routine
    status = models.CharField(
        max_length=20,
        choices=[('Not Started', 'Not Started'), ('In Progress', 'In Progress'), ('Completed', 'Completed')],
        default='Not Started'
    )
    updated_at = models.DateTimeField(auto_now=True)  # Track the last update time

    def __str__(self):
        return f"{self.user.username}'s progress on {self.routine.name} (Day {self.day})"


class ExerciseSetLog(models.Model):
    user_exercise_progress = models.ForeignKey(
        'UserExerciseProgress', 
        on_delete=models.CASCADE, 
        related_name='set_logs'
    )
    set_number = models.PositiveIntegerField()
    weight = models.FloatField()
    date = models.DateField(auto_now_add=True)

    def __str__(self):
        return f"Set {self.set_number} - {self.weight} kg"