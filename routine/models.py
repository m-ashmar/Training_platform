import os
import uuid
from django.core.files.storage import default_storage
from django.db import models, transaction
from django.conf import settings
from django.utils.timezone import now
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator, MaxValueValidator
from datetime import date
from django.db.models.signals import post_save, m2m_changed
from django.dispatch import receiver
import contextlib as _contextlib
import threading as _threading
import logging

logger = logging.getLogger(__name__)

# Thread-local switch used by suspend_progress_recalc() to mute the expensive
# per-row progress recomputation during bulk writes. Defined at module top so the
# receivers below can reference it unambiguously.
_recalc_state = _threading.local()


def exercise_image_upload_path(instance, filename):
    ext = filename.split('.')[-1]
    # Use uuid4 for unique filenames
    filename = f"exercise_{instance.pk or 'new'}_{uuid.uuid4().hex[:8]}.{ext}"
    return os.path.join('exercise_images', filename)

class Exercise(models.Model):
    """
    Exercise model representing individual exercises that can be used in routines.
    
    TODO: Add exercise categories and difficulty levels
    TODO: Implement exercise search and filtering
    TODO: Add exercise popularity metrics
    TODO: Consider adding exercise equipment requirements
    TODO: Consider allowing custom user-defined muscle groups in the future.
    """
    name = models.CharField(max_length=255, help_text="Exercise name")
    description = models.TextField(blank=True, null=True, help_text="Description of the exercise")
    
    # Exercise image (optional)
    image = models.ImageField(
        upload_to=exercise_image_upload_path,
        blank=True,
        null=True,
        help_text="Exercise demonstration image (optional)"
    )
    
    # Muscle group targeting (granular)
    target_muscle = models.CharField(
        max_length=50,
        choices=[
            ("Upper Chest", "Upper Chest"),
            ("Lower Chest", "Lower Chest"),
            ("Middle Chest", "Middle Chest"),
            ("Lateral Deltoid", "Lateral Deltoid"),
            ("Rear Deltoid", "Rear Deltoid"),
            ("Front Deltoid", "Front Deltoid"),
            ("Biceps", "Biceps"),
            ("Triceps", "Triceps"),
            ("Forearms", "Forearms"),
            ("Upper Back", "Upper Back"),
            ("Lats", "Lats"),
            ("Lower Back", "Lower Back"),
            ("Traps", "Traps"),
            ("Abdominals", "Abdominals"),
            ("Obliques", "Obliques"),
            ("Glutes", "Glutes"),
            ("Front Quads", "Front Quads"),
            ("Hamstrings", "Hamstrings"),
            ("Calves", "Calves"),
            ("Adductors", "Adductors"),
            ("Abductors", "Abductors"),
            ("Neck", "Neck"),
            ("Other", "Other")
        ],
        default="Other",
        help_text="Targeted muscle group for this exercise (granular)"
    )
    
    # Exercise metadata
    difficulty_level = models.CharField(
        max_length=20,
        choices=[
            ('beginner', 'Beginner'),
            ('intermediate', 'Intermediate'),
            ('advanced', 'Advanced'),
            ('expert', 'Expert')
        ],
        default='beginner',
        help_text="Exercise difficulty level"
    )
    
    # Trainer scoping - exercises can be global or trainer-specific
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_exercises',
        help_text="Trainer who created this exercise (null for global exercises)",
        limit_choices_to={'user_type': 'trainer'}
    )
    
    is_global = models.BooleanField(
        default=True,
        help_text="Whether this exercise is available to all trainers"
    )
    
    # System fields with temporary defaults for migration
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True, help_text="Whether this exercise is active")

    class Meta:
        verbose_name = "Exercise"
        verbose_name_plural = "Exercises"
        indexes = [
            models.Index(fields=['difficulty_level']),
            models.Index(fields=['created_by']),
            models.Index(fields=['is_global']),
            models.Index(fields=['is_active']),
            # Matches the real access pattern: WHERE <owner>=? ORDER BY created_at DESC, id DESC.
            # A single-column created_at index cannot serve that; this one can.
            models.Index(fields=['created_by', '-created_at', '-id'], name='exercise_owner_recent_idx'),
        ]
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['name', 'id']

    def __str__(self):
        return self.name

    def clean(self):
        """Validate exercise data."""
        if self.created_by and not self.created_by.is_trainer:
            raise ValidationError(_("Only trainers can create exercises"), code="trainer_required")
        
        if self.created_by and self.is_global:
            # If a trainer creates an exercise, it should not be global by default
            self.is_global = False

    def can_be_accessed_by(self, user):
        """
        Check if a user can access this exercise.
        
        Args:
            user: CustomUser instance
            
        Returns:
            bool: True if user can access this exercise
        """
        if not self.is_active:
            return False
            
        # Global exercises can be accessed by everyone
        if self.is_global:
            return True
            
        # Trainers can access their own exercises
        if self.created_by == user:
            return True
            
        # Admins can access all exercises
        if user.is_admin:
            return True
            
        return False

    def save(self, *args, **kwargs):
        # Clean up old exercise image if replaced
        if self.pk:
            try:
                old = Exercise.objects.get(pk=self.pk)
                if old.image and old.image != self.image:
                    if default_storage.exists(old.image.name):
                        default_storage.delete(old.image.name)
            except Exercise.DoesNotExist:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)
        super().save(*args, **kwargs)


class ExerciseMedia(models.Model):
    """
    Media associated with exercises (videos, photos, text descriptions).
    
    TODO: Add support for file uploads
    TODO: Implement media compression and optimization
    TODO: Add media validation and security checks
    """
    exercise = models.ForeignKey(Exercise, on_delete=models.CASCADE, related_name='media')
    media_type = models.CharField(
        max_length=10,
        choices=[('video', 'Video'), ('photo', 'Photo'), ('text', 'Text')],
        help_text="Type of media content"
    )
    content = models.TextField(help_text="URL for video/photo or text content")
    
    # Media metadata
    title = models.CharField(max_length=255, blank=True, help_text="Media title")
    description = models.TextField(blank=True, help_text="Media description")
    order = models.PositiveIntegerField(default=0, help_text="Display order")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Exercise Media"
        verbose_name_plural = "Exercise Media"
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['order', 'created_at', 'id']

    def __str__(self):
        return f"{self.media_type.capitalize()} for {self.exercise.name}"


class Routine(models.Model):
    """
    Enhanced Routine model supporting multi-trainer functionality.
    
    Routines can be created by trainers and assigned to their clients.
    Each routine is scoped to the trainer who created it.
    
    TODO: Add routine templates and cloning functionality
    TODO: Implement routine sharing between trainers
    TODO: Add routine analytics and performance tracking
    TODO: Consider adding routine difficulty progression
    """
    name = models.CharField(max_length=255, help_text="Routine name")
    description = models.TextField(blank=True, null=True, help_text="Optional description")
    
    # Translations for user-generated content
    translations = models.JSONField(
        default=dict,
        blank=True,
        help_text="JSON translations for dynamic user content (e.g., {'ar': {'name': '...', 'description': '...'}})"
    )
    
    # Trainer scoping - routines belong to specific trainers
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='created_routines',
        help_text="Trainer who created this routine",
        limit_choices_to={'user_type': 'trainer'}
    )
    
    # Client assignments - only clients of the trainer can be assigned
    assigned_to = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='assigned_routines',
        help_text="Clients assigned to this routine",
        limit_choices_to={'user_type': 'client'}
    )
    
    # Routine configuration
    is_active = models.BooleanField(default=True, help_text="Whether this routine is active")
    days = models.PositiveIntegerField(default=3, help_text="Number of days in the routine plan")
    
    # Scheduling
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    scheduled_date = models.DateTimeField(blank=True, null=True, help_text="Optional scheduling")
    start_date = models.DateField(default=date.today, help_text="Start date for the routine")
    end_date = models.DateField(blank=True, null=True, db_index=True, help_text="End date for the routine")
    
    # Routine metadata
    difficulty_level = models.CharField(
        max_length=20,
        choices=[
            ('beginner', 'Beginner'),
            ('intermediate', 'Intermediate'),
            ('advanced', 'Advanced'),
            ('expert', 'Expert')
        ],
        default='beginner',
        help_text="Routine difficulty level"
    )
    
    estimated_duration = models.PositiveIntegerField(
        default=60,
        help_text="Estimated duration in minutes"
    )
    
    # Exercises - scoped to trainer's accessible exercises
    exercises = models.ManyToManyField(Exercise, blank=True, help_text="Exercises in this routine")

    class Meta:
        verbose_name = "Routine"
        verbose_name_plural = "Routines"
        indexes = [
            models.Index(fields=['created_by']),
            models.Index(fields=['is_active']),
            models.Index(fields=['difficulty_level']),
            models.Index(fields=['start_date']),
            # Matches the real access pattern: WHERE <owner>=? ORDER BY created_at DESC, id DESC.
            # A single-column created_at index cannot serve that; this one can.
            models.Index(fields=['created_by', '-created_at', '-id'], name='routine_owner_recent_idx'),
        ]
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-created_at', '-id']

    def __str__(self):
        user_names = ', '.join(self.assigned_to.values_list('username', flat=True)[:5])
        extra = '' if self.assigned_to.count() <= 5 else '...'
        return f"{self.name} for {user_names}{extra}"

    def clean(self):
        """Validate routine data."""
        if not self.created_by.is_trainer:
            raise ValidationError(_("Only trainers can create routines"), code="trainer_required")
        
        # Ensure assigned users are clients of the trainer
        for user in self.assigned_to.all():
            if not user.is_client:
                raise ValidationError(_("User %(username)s is not a client") % {"username": user.username}, code="not_client")
            if user.assigned_trainer != self.created_by:
                raise ValidationError(_("User %(username)s is not assigned to trainer %(trainer)s") % {"username": user.username, "trainer": self.created_by.username}, code="not_assigned")

    def get_assigned_users(self):
        """Return a comma-separated list of usernames assigned to this routine."""
        return ", ".join(user.username for user in self.assigned_to.all())

    get_assigned_users.short_description = 'Assigned Users'

    def get_accessible_exercises(self):
        """
        Get exercises that can be used in this routine.
        
        Returns:
            QuerySet of accessible exercises
        """
        # Global exercises + trainer's own exercises
        return Exercise.objects.filter(
            models.Q(is_global=True) | models.Q(created_by=self.created_by)
        ).filter(is_active=True)

    def can_be_accessed_by(self, user):
        """
        Check if a user can access this routine.
        
        Args:
            user: CustomUser instance
            
        Returns:
            bool: True if user can access this routine
        """
        if not self.is_active:
            return False
            
        # Creator can access their own routines
        if self.created_by == user:
            return True
            
        # Assigned clients can access their routines
        if user in self.assigned_to.all():
            return True
            
        # Admins can access all routines
        if user.is_admin:
            return True
            
        return False

    def save(self, *args, **kwargs):
        """Override save to initialize progress for each user and day."""
        is_new = self.pk is None
        super().save(*args, **kwargs)
        
        # Scaffold rows are anchored to the routine's start_date. Actual training
        # sessions create/update the row for the date they happened on, so a routine
        # repeated over weeks now accumulates history instead of overwriting it.
        plan_date = self.start_date or timezone.localdate()
        if is_new:
            # Initialize progress for all users
            for user in self.assigned_to.all():
                for day in range(1, self.days + 1):
                    RoutineProgress.objects.get_or_create(
                        user=user, routine=self, day=day, date=plan_date,
                        defaults={'status': 'not_started'}
                    )
        # NOTE: no scaffolding branch for existing routines. It previously re-ran
        # get_or_create for every (user x day) on EVERY save — 142 queries just to
        # rename a routine with 20 clients x 7 days, scaling to ~700 at 100 clients.
        # New assignments are already scaffolded by the m2m_changed receiver below,
        # which is the correct trigger.

    def update_progress(self, user, day, status, progress_date=None):
        """Update or create progress for the user, day and calendar date."""
        if day < 1 or day > self.days:
            raise ValueError(f"Day {day} is out of range for this routine.")

        if progress_date is None:
            progress_date = timezone.localdate()

        progress_entry, created = RoutineProgress.objects.update_or_create(
            user=user,
            routine=self,
            day=day,
            date=progress_date,
            defaults={'status': status}
        )
        return {
            "day": progress_entry.day,
            "status": progress_entry.status,
            "created": created
        }


class RoutineExercise(models.Model):
    """
    Junction model linking routines and exercises with specific parameters.
    
    TODO: Add exercise order validation
    TODO: Implement exercise substitution logic
    TODO: Add exercise-specific notes and instructions
    """
    routine = models.ForeignKey(Routine, on_delete=models.CASCADE, related_name='routine_exercises')
    exercise = models.ForeignKey(Exercise, on_delete=models.CASCADE, related_name='routine_exercises')
    
    # Exercise parameters
    sets = models.PositiveIntegerField(default=3, null=True, blank=True, help_text="Number of sets")
    reps = models.PositiveIntegerField(default=10, null=True, blank=True, help_text="Repetitions per set")
    rest_time = models.IntegerField(default=60, null=True, blank=True, help_text="Rest time between sets in seconds")
    
    # Exercise scheduling
    day = models.PositiveIntegerField(default=1, help_text="Day of the routine")
    order = models.PositiveIntegerField(default=1, help_text="Order of the exercise in the routine")
    
    # Additional parameters
    weight = models.FloatField(null=True, blank=True, help_text="Weight in kg (if applicable)")
    duration = models.DurationField(null=True, blank=True, help_text="Duration for time-based exercises")
    distance = models.FloatField(null=True, blank=True, help_text="Distance in meters (if applicable)")
    
    # Notes and instructions
    notes = models.TextField(blank=True, help_text="Additional notes or instructions")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Routine Exercise"
        verbose_name_plural = "Routine Exercises"
        unique_together = [['routine', 'exercise', 'day', 'order']]
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['day', 'order', 'id']

    def __str__(self):
        return f"{self.exercise.name} in {self.routine.name} (Day {self.day}, Order {self.order})"

    def clean(self):
        """Validate routine exercise data."""
        if self.day > self.routine.days:
            raise ValidationError(_("Day %(day)s exceeds routine duration of %(days)s days") % {"day": self.day, "days": self.routine.days}, code="day_exceeded")
        
        # Ensure exercise is accessible to routine creator
        if not self.exercise.can_be_accessed_by(self.routine.created_by):
            raise ValidationError(_("Exercise %(name)s is not accessible to routine creator") % {"name": self.exercise.name}, code="exercise_not_accessible")


class UserExerciseProgress(models.Model):
    """
    Track user progress on individual exercises.
    
    TODO: Add progress analytics and trends
    TODO: Implement progress goals and milestones
    TODO: Add progress sharing between trainer and client
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='exercise_progress')
    exercise = models.ForeignKey(Exercise, on_delete=models.CASCADE, related_name='progress')
    
    # Progress data
    date = models.DateField(default=timezone.localdate, help_text="Date the progress is recorded")
    completed_sets = models.PositiveIntegerField(default=0, help_text="Number of sets completed")
    target_sets = models.PositiveIntegerField(default=0, help_text="Total sets required for the exercise")
    skipped = models.BooleanField(default=False, help_text="Whether the exercise was skipped")
    
    # Performance metrics
    total_weight = models.FloatField(default=0, help_text="Total weight lifted")
    total_repetitions = models.PositiveIntegerField(default=0, help_text="Total repetitions completed")
    duration = models.DurationField(null=True, blank=True, help_text="Time spent on exercise")
    
    # Notes
    notes = models.TextField(blank=True, help_text="User notes about the session")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Matches the real access pattern: WHERE <owner>=? ORDER BY created_at DESC, id DESC.
            # A single-column created_at index cannot serve that; this one can.
        indexes = [models.Index(fields=['user', '-created_at', '-id'], name='userexprog_recent_idx')]
        verbose_name = "User Exercise Progress"
        verbose_name_plural = "User Exercise Progress"
        unique_together = [['user', 'exercise', 'date']]
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-date', '-id']

    def __str__(self):
        return f"{self.user.username} - {self.exercise.name} on {self.date}"

    def calculate_training_volume(self):
        """Calculate total training volume based on set logs."""
        total_volume = sum(
            (log.weight or 0) * (log.reps or 0) 
            for log in self.set_logs.all()
        )
        return total_volume

    def can_be_accessed_by(self, user):
        """Check if a user can access this progress record."""
        # User can access their own progress
        if self.user == user:
            return True
        
        # Trainer can access their client's progress
        if user.is_trainer and self.user.assigned_trainer == user:
            return True
        
        # Admin can access all progress
        if user.is_admin:
            return True
        
        return False


class RoutineProgress(models.Model):
    """
    Track user progress on routines by day.
    
    TODO: Add progress analytics and reporting
    TODO: Implement progress notifications
    TODO: Add progress sharing features
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='routine_progress')
    # PROTECT, not CASCADE: deleting a routine used to silently erase every client's
    # recorded progress for it (set logs survive because they FK Exercise, leaving
    # history half-present and inconsistent). Trainers should deactivate a routine
    # (`is_active = False`) rather than destroy their clients' training record.
    routine = models.ForeignKey(Routine, on_delete=models.PROTECT, related_name='progress')
    
    # Progress data
    day = models.PositiveIntegerField(help_text="Day of the routine")
    status = models.CharField(
        max_length=20,
        choices=[
            # Stored values are lowercase snake_case, matching the convention already
            # used by WorkoutSession, social, subscription and analytics. RoutineProgress
            # was the lone outlier storing Title Case, which forced any API consumer to
            # implement two vocabularies for the same concept.
            ('not_started', 'Not Started'),
            ('in_progress', 'In Progress'),
            ('completed', 'Completed'),
            ('skipped', 'Skipped')
        ],
        default='not_started',
        help_text="Progress status for this day"
    )
    
    # Performance metrics
    completion_time = models.DurationField(null=True, blank=True, help_text="Time taken to complete")
    exercises_completed = models.PositiveIntegerField(default=0, help_text="Number of exercises completed")
    total_exercises = models.PositiveIntegerField(default=0, help_text="Total exercises for this day")
    
    # Notes
    notes = models.TextField(blank=True, help_text="User notes about the session")

    # The calendar day this progress belongs to.
    # Without this the uniqueness key was (user, routine, day), so a routine
    # repeated weekly could only ever hold N rows — every repeat OVERWROTE the
    # previous session and training history was destroyed. `updated_at` is
    # auto_now and therefore useless as a training date (editing an old record
    # moved that workout to today).
    date = models.DateField(
        default=timezone.localdate, db_index=True,
        help_text="Calendar date this progress entry belongs to"
    )

    updated_at = models.DateTimeField(auto_now=True, help_text="Last update time")

    class Meta:
        verbose_name = "Routine Progress"
        verbose_name_plural = "Routine Progress"
        unique_together = [['user', 'routine', 'day', 'date']]
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-date', 'day', '-id']
        indexes = [
            models.Index(fields=['user', 'date']),
            models.Index(fields=['user', 'status', 'date']),
        ]

    def __str__(self):
        return f"{self.user.username}'s progress on {self.routine.name} (Day {self.day})"

    def can_be_accessed_by(self, user):
        """Check if a user can access this progress record."""
        # User can access their own progress
        if self.user == user:
            return True
        
        # Trainer can access their client's progress
        if user.is_trainer and self.user.assigned_trainer == user:
            return True
        
        # Admin can access all progress
        if user.is_admin:
            return True
        
        return False

    @property
    def completion_percentage(self):
        """Calculate completion percentage for this day."""
        if self.total_exercises == 0:
            return 0
        return (self.exercises_completed / self.total_exercises) * 100


class WorkoutSession(models.Model):
    """
    Represents a user's workout session. Tracks start/end and status for real-time execution.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='workout_sessions')
    routine = models.ForeignKey(Routine, on_delete=models.CASCADE, related_name='workout_sessions')
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=[('active', 'Active'), ('completed', 'Completed'), ('abandoned', 'Abandoned')], default='active')
    
    class Meta:
        verbose_name = "Workout Session"
        verbose_name_plural = "Workout Sessions"
        indexes = [
            models.Index(fields=['user', 'start_time']),
            models.Index(fields=['routine']),
            models.Index(fields=['status']),
            # Composite index for recent-progress aggregation query
            models.Index(fields=['user', 'status', 'start_time'], name='ws_user_status_start_idx'),
            # Matches the real access pattern: WHERE user=? ORDER BY start_time DESC, id DESC.
            # Measured on a power user with 5,050 rows: 0.682 ms -> 0.089 ms, because
            # the planner stops sorting their entire history to return 25 rows.
            models.Index(fields=['user', '-start_time', '-id'], name='workoutsess_recent_idx'),
        ]
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-start_time', '-id']
    
    def __str__(self):
        return f"Session for {self.user.username} ({self.routine.name}) at {self.start_time}"

    @property
    def duration(self):
        """Calculate duration of the session."""
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return None


class ExerciseSetLog(models.Model):
    """
    Detailed log of individual sets within an exercise session.
    
    TODO: Add set validation and quality metrics
    TODO: Implement set progression tracking
    TODO: Add set-specific notes and feedback
    """
    user_exercise_progress = models.ForeignKey(
        'UserExerciseProgress',
        on_delete=models.CASCADE,
        related_name='set_logs',
        null=True, blank=True,  # TODO: Backfill and make non-nullable if possible
        help_text="User's exercise progress for this set log"
    )
    workout_session = models.ForeignKey('WorkoutSession', on_delete=models.CASCADE, related_name='set_logs', null=True, blank=True, help_text="Session this set belongs to")
    
    # Set data
    set_number = models.PositiveIntegerField(null=True, blank=True, help_text="Set number in the sequence")
    weight = models.FloatField(null=True, blank=True, help_text="Weight used in kg")
    reps = models.PositiveIntegerField(default=10, null=True, blank=True, help_text="Repetitions completed")
    
    # Performance metrics
    rest_time = models.IntegerField(null=True, blank=True, help_text="Rest time before this set in seconds")
    duration = models.DurationField(null=True, blank=True, help_text="Time taken for this set")
    
    # Quality metrics
    rpe = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Rate of Perceived Exertion (1-10)",
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    
    # Notes
    notes = models.TextField(blank=True, help_text="Notes about this specific set")
    
    date = models.DateField(default=timezone.localdate, null=True, blank=True, help_text="Date of the set")  # TODO: Backfill and make non-nullable if desired

    class Meta:
        verbose_name = "Exercise Set Log"
        verbose_name_plural = "Exercise Set Logs"
        indexes = [
            models.Index(fields=['workout_session']),
            models.Index(fields=['date']),
            models.Index(fields=['user_exercise_progress']),
        ]
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['set_number', 'id']

    def __str__(self):
        return f"Set {self.set_number} - {self.weight} kg x {self.reps} reps"

    def clean(self):
        """Validate set log data."""
        if self.rpe and (self.rpe < 1 or self.rpe > 10):
            raise ValidationError(_("RPE must be between 1 and 10"), code="rpe_out_of_range")



class RoutineTemplate(models.Model):
    """
    RoutineTemplate: reusable structure for common training splits, saved by trainers.
    Trainers can organize by goal (hypertrophy, strength, etc.).
    
    Visibility Rules:
    - Public templates: Visible to all authenticated users
    - Private templates: Only visible to the creator (trainer)
    - Admins can see all templates
    
    TODO: Add tags, variants, equipment, etc.
    """
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    goal = models.CharField(max_length=100, help_text="e.g. Hypertrophy, Strength, Endurance")
    days = models.PositiveIntegerField(default=3, help_text="Number of days in the template plan")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='created_templates')
    is_public = models.BooleanField(default=False, help_text="Whether this template is visible to all users")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # ManyToMany to Exercise through RoutineTemplateExercise
    exercises = models.ManyToManyField('Exercise', through='RoutineTemplateExercise', related_name='templates')

    class Meta:
        # Matches the real access pattern: WHERE <owner>=? ORDER BY created_at DESC, id DESC.
            # A single-column created_at index cannot serve that; this one can.
        indexes = [models.Index(fields=['created_by', '-created_at', '-id'], name='routinetmpl_recent_idx')]
        verbose_name = "Routine Template"
        verbose_name_plural = "Routine Templates"
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f"{self.name} ({self.goal}) by {self.created_by.username}"

    def can_be_accessed_by(self, user):
        """
        Check if a user can access this template.
        
        Args:
            user: CustomUser instance
            
        Returns:
            bool: True if user can access this template
        """
        # Admins can access all templates
        if user.is_admin:
            return True
        
        # Public templates can be accessed by everyone
        if self.is_public:
            return True
        
        # Private templates can only be accessed by the creator
        if self.created_by == user:
            return True
        
        return False

    def clean(self):
        """Validate template data."""
        if not self.created_by.is_trainer:
            raise ValidationError(_("Only trainers can create templates"), code="trainer_required")


class RoutineTemplateExercise(models.Model):
    """
    Through model for RoutineTemplate <-> Exercise, with sets, reps, rest, and order.
    TODO: Add tempo, notes, etc.
    """
    template = models.ForeignKey(RoutineTemplate, on_delete=models.CASCADE)
    exercise = models.ForeignKey('Exercise', on_delete=models.CASCADE)
    sets = models.PositiveIntegerField(default=3)
    reps = models.PositiveIntegerField(default=10)
    rest_time = models.PositiveIntegerField(default=90, help_text="Rest time in seconds")
    day = models.PositiveIntegerField(default=1, help_text="Day of the template")
    order = models.PositiveIntegerField(default=1)

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-id']
    # TODO: Add more fields as needed

# --- SIGNALS FOR ROUTINE PROGRESS AUTO-UPDATE ---

@receiver(post_save, sender=UserExerciseProgress)
def update_routine_progress_on_exercise_progress(sender, instance, created, **kwargs):
    """
    When a UserExerciseProgress is created or updated, update the corresponding RoutineProgress.
    Correctly handles completion logic even when target_sets is 0.
    Optimized to avoid N+1 queries.
    """
    # Honour the bulk-write suspension (see suspend_progress_recalc).
    if getattr(_recalc_state, 'suspended', False) and not kwargs.get('_forced'):
        return
    user = instance.user
    exercise = instance.exercise
    progress_date = instance.date
    
    # Pre-fetch the related routine_exercises to avoid N+1 down the line
    routines = Routine.objects.filter(
        assigned_to=user, 
        routine_exercises__exercise=exercise,
        is_active=True
    ).prefetch_related('routine_exercises', 'routine_exercises__exercise').distinct()
    
    for routine in routines:
        # All routine exercises are loaded in memory now
        exercises_in_routine = list(routine.routine_exercises.all())
        
        # Find which days this exercise appears in this routine
        days_with_exercise = {rex.day for rex in exercises_in_routine if rex.exercise_id == exercise.id}
        
        for day in days_with_exercise:
            # Get all exercises scheduled for this specific day in the routine (from memory)
            day_exercises = [rex.exercise for rex in exercises_in_routine if rex.day == day]
            total_exercises_count = len(day_exercises)
            
            if total_exercises_count == 0:
                continue

            completed_count = 0
            
            # Bulk fetch progress for all exercises in this day for the user
            day_exercise_ids = [ex.id for ex in day_exercises]
            progress_records = list(UserExerciseProgress.objects.filter(
                user=user, 
                exercise_id__in=day_exercise_ids, 
                date=progress_date, 
                skipped=False
            ).order_by('-updated_at'))
            
            # Assuming newest updated record for each exercise is what we want,
            # we build a dict mapping exercise_id to its most recent progress record
            progress_by_exercise = {}
            for record in progress_records:
                if record.exercise_id not in progress_by_exercise:
                    progress_by_exercise[record.exercise_id] = record

            # Check completion status for each exercise in this day
            for day_ex in day_exercises:
                user_prog = progress_by_exercise.get(day_ex.id)
                
                if user_prog is not None:
                    if user_prog.target_sets > 0:
                        if user_prog.completed_sets >= user_prog.target_sets:
                            completed_count += 1
                    else:
                        if user_prog.completed_sets > 0:
                            completed_count += 1
            
            # Determine status based on counts
            if completed_count == 0:
                status = 'not_started'
            elif completed_count == total_exercises_count:
                status = 'completed'
            else:
                status = 'in_progress'
            
            # Update or create the RoutineProgress record
            RoutineProgress.objects.update_or_create(
                user=user,
                routine=routine,
                day=day,
                date=progress_date,
                defaults={
                    'status': status,
                    'exercises_completed': completed_count,
                    'total_exercises': total_exercises_count,
                }
            )

@_contextlib.contextmanager
def suspend_progress_recalc():
    """
    Suppress the per-set-log RoutineProgress recomputation for the duration of a
    bulk operation, so it runs ONCE at the end instead of once per row.

    The receiver below costs ~16 queries per set log. Logging a normal workout
    (10 sets x 3 exercises = 30 logs) cost 493 queries before this existed.
    Thread-local so concurrent requests are unaffected.
    """
    prev = getattr(_recalc_state, 'suspended', False)
    _recalc_state.suspended = True
    try:
        yield
    finally:
        _recalc_state.suspended = prev


def recalc_progress_for(progress):
    """Run the recomputation explicitly (used after a suspended bulk write)."""
    update_routine_progress_on_set_log(
        sender=ExerciseSetLog, instance=progress.set_logs.first(), created=False, _forced=True
    ) if progress.set_logs.exists() else None


@receiver(post_save, sender=ExerciseSetLog)
def update_routine_progress_on_set_log(sender, instance, created, **kwargs):
    """
    When an ExerciseSetLog is created or updated, trigger the RoutineProgress update.
    This ensures that adding sets (which updates completed_sets in UserExerciseProgress)
    propagates to the overall routine status.
    """
    if getattr(_recalc_state, 'suspended', False) and not kwargs.get('_forced'):
        return
    progress = instance.user_exercise_progress
    if not progress:
        return
        
    # Defer to after commit. post_save fires INSIDE the insert's transaction, so the
    # aggregate below could not see rows other writers had not yet committed — the row
    # lock alone still produced 400/450/300 where 600 was correct. Running after commit
    # means every recalc reads committed data, and the last one to take the lock sees
    # all of them.
    transaction.on_commit(lambda pk=progress.pk: _recalc_locked(pk))


def _recalc_locked(progress_pk):
    """Recompute a UserExerciseProgress' derived totals in ONE atomic statement.

    Every earlier shape of this raced. A read-modify-write via `progress.save()` let
    concurrent writers clobber each other; adding `select_for_update` did not help
    because the recalc ran inside the insert's own transaction and could not see
    uncommitted rows; deferring to `on_commit` still left a window between the
    aggregate SELECT and the UPDATE, where another writer could land in between.
    Logging 12 sets concurrently produced totals of 150/200/300/400/450/550 where 600
    was correct — all 12 rows persisted, only the derived columns were wrong.

    A single UPDATE with correlated subqueries removes the window entirely: the
    database computes the aggregate at write time, under the row's own lock, so the
    last statement to execute reflects every committed set log.
    """
    from django.db.models import Count, OuterRef, Subquery, Sum, Value
    from django.db.models.functions import Coalesce

    logs = ExerciseSetLog.objects.filter(user_exercise_progress=OuterRef('pk'))

    def agg(expr, out):
        return Coalesce(
            Subquery(
                logs.values('user_exercise_progress').annotate(v=expr).values('v')[:1],
                output_field=out,
            ),
            Value(0, output_field=out),
        )

    UserExerciseProgress.objects.filter(pk=progress_pk).update(
        completed_sets=agg(Count('id'), models.IntegerField()),
        total_weight=agg(Sum('weight'), models.FloatField()),
        total_repetitions=agg(Sum('reps'), models.IntegerField()),
    )


# --- M2M SIGNAL FOR AUTOMATIC ROUTINEPROGRESS CREATION ---

@receiver(m2m_changed, sender=Routine.assigned_to.through)
def create_routine_progress_on_assignment(sender, instance, action, pk_set, **kwargs):
    """
    Automatically create RoutineProgress records when users are assigned to routines.
    
    This signal handles all M2M changes to Routine.assigned_to field:
    - post_add: Users added to routine -> Create RoutineProgress for each day
    - post_remove: Users removed from routine -> Optionally clean up progress
    
    This ensures RoutineProgress records exist regardless of how the assignment happens
    (views, admin, shell, etc.).
    """
    if action == 'post_add' and pk_set:
        from users.models import CustomUser
        
        routine = instance
        
        # Get newly added users
        new_users = CustomUser.objects.filter(pk__in=pk_set)
        
        for user in new_users:
            for day in range(1, routine.days + 1):
                # Get count of exercises for this day
                exercises_count = routine.routine_exercises.filter(day=day).count()
                
                # Create progress record if it doesn't exist
                RoutineProgress.objects.get_or_create(
                    user=user,
                    routine=routine,
                    day=day,
                    date=(routine.start_date or timezone.localdate()),
                    defaults={
                        'status': 'not_started',
                        'exercises_completed': 0,
                        'total_exercises': exercises_count
                    }
                )
    
    elif action == 'post_remove' and pk_set:
        # Optional: Clean up RoutineProgress when users are unassigned
        # Currently keeping progress records for historical data
        # Uncomment below to delete progress on unassignment:
        # RoutineProgress.objects.filter(
        #     user_id__in=pk_set,
        #     routine=instance
        # ).delete()
        pass


