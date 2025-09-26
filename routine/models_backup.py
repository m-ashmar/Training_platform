from django.db import models
from django.conf import settings
from django.utils.timezone import now
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db.models import Sum, Avg, Max, Min, Count
from django.db.models.functions import TruncDate, TruncWeek, TruncMonth


class Exercise(models.Model):
    """
    Exercise model representing individual exercises that can be used in routines.
    
    TODO: Add exercise categories and difficulty levels
    TODO: Implement exercise search and filtering
    TODO: Add exercise popularity metrics
    TODO: Consider adding exercise equipment requirements
    """
    name = models.CharField(max_length=255, help_text="Exercise name")
    description = models.TextField(blank=True, null=True, help_text="Description of the exercise")
    
    # Exercise metadata
    category = models.CharField(
        max_length=50,
        choices=[
            ('strength', 'Strength Training'),
            ('cardio', 'Cardiovascular'),
            ('flexibility', 'Flexibility'),
            ('balance', 'Balance'),
            ('sports', 'Sports Specific'),
            ('rehabilitation', 'Rehabilitation'),
            ('other', 'Other')
        ],
        default='strength',
        help_text="Exercise category"
    )
    
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
            models.Index(fields=['category']),
            models.Index(fields=['difficulty_level']),
            models.Index(fields=['created_by']),
            models.Index(fields=['is_global']),
            models.Index(fields=['is_active']),
        ]
        ordering = ['name']

    def __str__(self):
        return self.name

    def clean(self):
        """Validate exercise data."""
        if self.created_by and not self.created_by.is_trainer:
            raise ValidationError("Only trainers can create exercises")
        
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
        ordering = ['order', 'created_at']

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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    scheduled_date = models.DateTimeField(blank=True, null=True, help_text="Optional scheduling")
    start_date = models.DateField(default=now, help_text="Start date for the routine")
    end_date = models.DateField(blank=True, null=True, help_text="End date for the routine")
    
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
        ]
        ordering = ['-created_at']

    def __str__(self):
        user_names = ', '.join(self.assigned_to.values_list('username', flat=True)[:5])
        extra = '' if self.assigned_to.count() <= 5 else '...'
        return f"{self.name} for {user_names}{extra}"

    def clean(self):
        """Validate routine data."""
        if not self.created_by.is_trainer:
            raise ValidationError("Only trainers can create routines")
        
        # Ensure assigned users are clients of the trainer
        for user in self.assigned_to.all():
            if not user.is_client:
                raise ValidationError(f"User {user.username} is not a client")
            if user.assigned_trainer != self.created_by:
                raise ValidationError(f"User {user.username} is not assigned to trainer {self.created_by.username}")

    def get_assigned_users(self):
        """Return a comma-separated list of usernames assigned to this routine."""
        return ", ".join(user.username for user in self.assigned_to.all())

    def get_accessible_exercises(self):
        """
        Get exercises that can be used in this routine.
        
        Returns:
            QuerySet: Exercises accessible to the routine creator
        """
        if self.created_by.is_admin:
            return Exercise.objects.filter(is_active=True)
        else:
            return Exercise.objects.filter(
                models.Q(is_global=True) | models.Q(created_by=self.created_by),
                is_active=True
            )

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
            
        # Admins can access all routines
        if user.is_admin:
            return True
            
        # Trainers can access routines they created
        if self.created_by == user:
            return True
            
        # Clients can access routines assigned to them
        if user in self.assigned_to.all():
            return True
            
        return False

    def save(self, *args, **kwargs):
        """Override save to ensure proper validation."""
        self.clean()
        super().save(*args, **kwargs)

    def update_progress(self, user, day, status):
        """
        Update progress for a specific user and day.
        
        Args:
            user: CustomUser instance
            day: Day number in the routine
            status: Progress status
        """
        progress, created = RoutineProgress.objects.get_or_create(
            user=user,
            routine=self,
            day=day,
            defaults={'status': status}
        )
        if not created:
            progress.status = status
            progress.save()


class RoutineExercise(models.Model):
    """
    Enhanced junction model linking routines and exercises with specific parameters.
    
    Now includes sets, reps, rest_time, and order fields as requested.
    
    TODO: Add exercise order validation
    TODO: Implement exercise substitution logic
    TODO: Add exercise-specific notes and instructions
    """
    routine = models.ForeignKey(Routine, on_delete=models.CASCADE, related_name='routine_exercises')
    exercise = models.ForeignKey(Exercise, on_delete=models.CASCADE, related_name='routine_exercises')
    
    # Enhanced exercise parameters with required fields
    sets = models.PositiveIntegerField(
        default=3, 
        help_text="Number of sets for this exercise",
        validators=[MinValueValidator(1)]
    )
    reps = models.PositiveIntegerField(
        default=10, 
        help_text="Repetitions per set",
        validators=[MinValueValidator(1)]
    )
    rest_time = models.PositiveIntegerField(
        default=60, 
        help_text="Rest time between sets in seconds",
        validators=[MinValueValidator(0)]
    )
    order = models.PositiveIntegerField(
        default=1, 
        help_text="Order of the exercise in the routine",
        validators=[MinValueValidator(1)]
    )
    
    # Exercise scheduling
    day = models.PositiveIntegerField(
        default=1, 
        help_text="Day of the routine",
        validators=[MinValueValidator(1)]
    )
    
    # Additional parameters (optional)
    weight = models.FloatField(
        null=True, 
        blank=True, 
        help_text="Weight in kg (if applicable)"
    )
    duration = models.DurationField(
        null=True, 
        blank=True, 
        help_text="Duration for time-based exercises"
    )
    distance = models.FloatField(
        null=True, 
        blank=True, 
        help_text="Distance in meters (if applicable)"
    )
    
    # Notes and instructions
    notes = models.TextField(blank=True, help_text="Additional notes or instructions")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Routine Exercise"
        verbose_name_plural = "Routine Exercises"
        unique_together = [['routine', 'exercise', 'day', 'order']]
        ordering = ['day', 'order']
        indexes = [
            models.Index(fields=['routine', 'day']),
            models.Index(fields=['exercise']),
            models.Index(fields=['order']),
        ]

    def __str__(self):
        return f"{self.exercise.name} - Day {self.day}, Order {self.order}"

    def clean(self):
        """Validate routine exercise data."""
        if self.sets < 1:
            raise ValidationError("Sets must be at least 1")
        if self.reps < 1:
            raise ValidationError("Reps must be at least 1")
        if self.order < 1:
            raise ValidationError("Order must be at least 1")
        if self.day < 1:
            raise ValidationError("Day must be at least 1")


class WorkoutSession(models.Model):
    """
    Real-time workout session tracking model.
    
    Tracks the start and end of workout sessions for users.
    Each session can contain multiple exercises with their sets.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='workout_sessions',
        help_text="User performing the workout"
    )
    
    routine = models.ForeignKey(
        Routine,
        on_delete=models.CASCADE,
        related_name='workout_sessions',
        null=True,
        blank=True,
        help_text="Routine being followed (optional)"
    )
    
    # Session timing
    started_at = models.DateTimeField(auto_now_add=True, help_text="When the session started")
    ended_at = models.DateTimeField(
        null=True, 
        blank=True, 
        help_text="When the session ended"
    )
    
    # Session status
    status = models.CharField(
        max_length=20,
        choices=[
            ('active', 'Active'),
            ('completed', 'Completed'),
            ('paused', 'Paused'),
            ('cancelled', 'Cancelled')
        ],
        default='active',
        help_text="Current status of the workout session"
    )
    
    # Session metadata
    notes = models.TextField(blank=True, help_text="User notes about the session")
    total_duration = models.DurationField(
        null=True, 
        blank=True, 
        help_text="Total duration of the session"
    )
    
    # Performance tracking
    total_volume = models.FloatField(
        default=0.0, 
        help_text="Total training volume (weight × reps) for this session"
    )
    exercises_completed = models.PositiveIntegerField(
        default=0, 
        help_text="Number of exercises completed"
    )
    sets_completed = models.PositiveIntegerField(
        default=0, 
        help_text="Number of sets completed"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Workout Session"
        verbose_name_plural = "Workout Sessions"
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['user', 'started_at']),
            models.Index(fields=['status']),
            models.Index(fields=['routine']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.started_at.strftime('%Y-%m-%d %H:%M')}"

    def end_session(self):
        """End the workout session and calculate totals."""
        if self.status == 'active':
            self.ended_at = timezone.now()
            self.status = 'completed'
            self.total_duration = self.ended_at - self.started_at
            self.save()
            
            # Recalculate totals from actual exercise logs
            self._recalculate_totals()

    def _recalculate_totals(self):
        """Recalculate session totals from exercise logs."""
        # Get all set logs for this session
        set_logs = ExerciseSetLog.objects.filter(
            workout_session=self
        )
        
        # Calculate totals
        self.total_volume = sum(log.calculate_volume() for log in set_logs)
        self.exercises_completed = set_logs.values('exercise').distinct().count()
        self.sets_completed = set_logs.count()
        
        self.save(update_fields=['total_volume', 'exercises_completed', 'sets_completed'])

    @property
    def is_active(self):
        """Check if the session is currently active."""
        return self.status == 'active'

    @property
    def duration(self):
        """Get the current duration of the session."""
        if self.ended_at:
            return self.ended_at - self.started_at
        return timezone.now() - self.started_at


class ExerciseSetLog(models.Model):
    """
    Enhanced detailed log of individual sets within an exercise session.
    
    Now tracks real-time execution with actual performance data.
    
    TODO: Add set validation and quality metrics
    TODO: Implement set progression tracking
    TODO: Add set-specific notes and feedback
    """
    # Session tracking
    workout_session = models.ForeignKey(
        WorkoutSession,
        on_delete=models.CASCADE,
        related_name='set_logs',
        help_text="Workout session this set belongs to"
    )
    
    # Exercise identification
    exercise = models.ForeignKey(
        Exercise,
        on_delete=models.CASCADE,
        related_name='set_logs',
        help_text="Exercise performed"
    )
    
    routine_exercise = models.ForeignKey(
        RoutineExercise,
        on_delete=models.CASCADE,
        related_name='set_logs',
        null=True,
        blank=True,
        help_text="Routine exercise definition (if following a routine)"
    )
    
    # Set data with enhanced tracking
    set_number = models.PositiveIntegerField(
        help_text="Set number in the sequence",
        validators=[MinValueValidator(1)]
    )
    
    # Target vs actual performance
    target_weight = models.FloatField(
        null=True, 
        blank=True, 
        help_text="Target weight in kg"
    )
    actual_weight = models.FloatField(
        help_text="Actual weight used in kg",
        validators=[MinValueValidator(0)]
    )
    
    target_reps = models.PositiveIntegerField(
        null=True, 
        blank=True, 
        help_text="Target repetitions"
    )
    actual_reps = models.PositiveIntegerField(
        help_text="Actual repetitions completed",
        validators=[MinValueValidator(0)]
    )
    
    # Timing and rest tracking
    rest_time_before = models.PositiveIntegerField(
        null=True, 
        blank=True, 
        help_text="Rest time before this set in seconds"
    )
    set_duration = models.DurationField(
        null=True, 
        blank=True, 
        help_text="Time taken for this set"
    )
    
    # Performance metrics
    rpe = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Rate of Perceived Exertion (1-10)",
        validators=[MinValueValidator(1), MaxValueValidator(10)]
    )
    
    # Set status
    status = models.CharField(
        max_length=20,
        choices=[
            ('completed', 'Completed'),
            ('failed', 'Failed'),
            ('skipped', 'Skipped'),
            ('partial', 'Partial')
        ],
        default='completed',
        help_text="Status of this set"
    )
    
    # Notes
    notes = models.TextField(blank=True, help_text="Notes about this specific set")
    
    # Timestamps
    started_at = models.DateTimeField(
        auto_now_add=True, 
        help_text="When this set started"
    )
    completed_at = models.DateTimeField(
        null=True, 
        blank=True, 
        help_text="When this set was completed"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Exercise Set Log"
        verbose_name_plural = "Exercise Set Logs"
        ordering = ['set_number']
        indexes = [
            models.Index(fields=['workout_session', 'exercise']),
            models.Index(fields=['exercise', 'started_at']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"{self.exercise.name} - Set {self.set_number} ({self.actual_weight}kg x {self.actual_reps})"

    def complete_set(self):
        """Mark the set as completed and record completion time."""
        if self.status == 'completed':
            self.completed_at = timezone.now()
            if self.started_at:
                self.set_duration = self.completed_at - self.started_at
            self.save()

    def calculate_volume(self):
        """Calculate the training volume for this set."""
        return self.actual_weight * self.actual_reps

    def clean(self):
        """Validate set log data."""
        if self.actual_weight < 0:
            raise ValidationError("Weight cannot be negative")
        if self.actual_reps < 0:
            raise ValidationError("Reps cannot be negative")
        if self.rpe and (self.rpe < 1 or self.rpe > 10):
            raise ValidationError("RPE must be between 1 and 10")


class TrainingVolume(models.Model):
    """
    Aggregated training volume tracking for performance analytics.
    
    Automatically calculates and stores daily, weekly, and monthly training volumes
    for efficient reporting and analytics.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='training_volumes',
        help_text="User whose volume is being tracked"
    )
    
    # Time period
    date = models.DateField(help_text="Date for this volume record")
    period_type = models.CharField(
        max_length=10,
        choices=[
            ('daily', 'Daily'),
            ('weekly', 'Weekly'),
            ('monthly', 'Monthly')
        ],
        help_text="Type of time period"
    )
    
    # Volume metrics
    total_volume = models.FloatField(
        default=0.0, 
        help_text="Total training volume (weight × reps)"
    )
    total_sets = models.PositiveIntegerField(
        default=0, 
        help_text="Total number of sets"
    )
    total_reps = models.PositiveIntegerField(
        default=0, 
        help_text="Total number of repetitions"
    )
    total_exercises = models.PositiveIntegerField(
        default=0, 
        help_text="Total number of exercises performed"
    )
    total_workouts = models.PositiveIntegerField(
        default=0, 
        help_text="Total number of workout sessions"
    )
    
    # Performance metrics
    average_weight = models.FloatField(
        default=0.0, 
        help_text="Average weight per set"
    )
    average_reps = models.FloatField(
        default=0.0, 
        help_text="Average reps per set"
    )
    
    # Personal records tracking
    is_personal_record = models.BooleanField(
        default=False, 
        help_text="Whether this period set a personal record"
    )
    previous_record = models.FloatField(
        null=True, 
        blank=True, 
        help_text="Previous personal record for comparison"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Training Volume"
        verbose_name_plural = "Training Volumes"
        unique_together = [['user', 'date', 'period_type']]
        ordering = ['-date']
        indexes = [
            models.Index(fields=['user', 'date']),
            models.Index(fields=['user', 'period_type']),
            models.Index(fields=['is_personal_record']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.period_type} {self.date} ({self.total_volume}kg)"

    @classmethod
    def calculate_daily_volume(cls, user, date):
        """Calculate and update daily training volume for a user."""
        # Get all set logs for the user on the specified date
        set_logs = ExerciseSetLog.objects.filter(
            workout_session__user=user,
            started_at__date=date,
            status='completed'
        )
        
        if not set_logs.exists():
            return None
        
        # Calculate totals
        total_volume = sum(log.calculate_volume() for log in set_logs)
        total_sets = set_logs.count()
        total_reps = sum(log.actual_reps for log in set_logs)
        total_exercises = set_logs.values('exercise').distinct().count()
        total_workouts = set_logs.values('workout_session').distinct().count()
        
        # Calculate averages
        average_weight = sum(log.actual_weight for log in set_logs) / total_sets if total_sets > 0 else 0
        average_reps = total_reps / total_sets if total_sets > 0 else 0
        
        # Check if this is a personal record
        previous_record = cls.objects.filter(
            user=user,
            period_type='daily'
        ).exclude(date=date).aggregate(Max('total_volume'))['total_volume__max']
        
        is_personal_record = previous_record is None or total_volume > previous_record
        
        # Create or update the volume record
        volume_record, created = cls.objects.get_or_create(
            user=user,
            date=date,
            period_type='daily',
            defaults={
                'total_volume': total_volume,
                'total_sets': total_sets,
                'total_reps': total_reps,
                'total_exercises': total_exercises,
                'total_workouts': total_workouts,
                'average_weight': average_weight,
                'average_reps': average_reps,
                'is_personal_record': is_personal_record,
                'previous_record': previous_record
            }
        )
        
        if not created:
            volume_record.total_volume = total_volume
            volume_record.total_sets = total_sets
            volume_record.total_reps = total_reps
            volume_record.total_exercises = total_exercises
            volume_record.total_workouts = total_workouts
            volume_record.average_weight = average_weight
            volume_record.average_reps = average_reps
            volume_record.is_personal_record = is_personal_record
            volume_record.previous_record = previous_record
            volume_record.save()
        
        return volume_record

    @classmethod
    def calculate_weekly_volume(cls, user, week_start_date):
        """Calculate and update weekly training volume for a user."""
        from datetime import timedelta
        
        week_end_date = week_start_date + timedelta(days=6)
        
        # Get all set logs for the user in the specified week
        set_logs = ExerciseSetLog.objects.filter(
            workout_session__user=user,
            started_at__date__range=[week_start_date, week_end_date],
            status='completed'
        )
        
        if not set_logs.exists():
            return None
        
        # Calculate totals (same logic as daily)
        total_volume = sum(log.calculate_volume() for log in set_logs)
        total_sets = set_logs.count()
        total_reps = sum(log.actual_reps for log in set_logs)
        total_exercises = set_logs.values('exercise').distinct().count()
        total_workouts = set_logs.values('workout_session').distinct().count()
        
        average_weight = sum(log.actual_weight for log in set_logs) / total_sets if total_sets > 0 else 0
        average_reps = total_reps / total_sets if total_sets > 0 else 0
        
        # Check for personal record
        previous_record = cls.objects.filter(
            user=user,
            period_type='weekly'
        ).exclude(date=week_start_date).aggregate(Max('total_volume'))['total_volume__max']
        
        is_personal_record = previous_record is None or total_volume > previous_record
        
        # Create or update the volume record
        volume_record, created = cls.objects.get_or_create(
            user=user,
            date=week_start_date,
            period_type='weekly',
            defaults={
                'total_volume': total_volume,
                'total_sets': total_sets,
                'total_reps': total_reps,
                'total_exercises': total_exercises,
                'total_workouts': total_workouts,
                'average_weight': average_weight,
                'average_reps': average_reps,
                'is_personal_record': is_personal_record,
                'previous_record': previous_record
            }
        )
        
        if not created:
            volume_record.total_volume = total_volume
            volume_record.total_sets = total_sets
            volume_record.total_reps = total_reps
            volume_record.total_exercises = total_exercises
            volume_record.total_workouts = total_workouts
            volume_record.average_weight = average_weight
            volume_record.average_reps = average_reps
            volume_record.is_personal_record = is_personal_record
            volume_record.previous_record = previous_record
            volume_record.save()
        
        return volume_record


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
    date = models.DateField(default=now, help_text="Date the progress is recorded")
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
        verbose_name = "User Exercise Progress"
        verbose_name_plural = "User Exercise Progress"
        unique_together = [['user', 'exercise', 'date']]
        ordering = ['-date']

    def __str__(self):
        return f"{self.user.username} - {self.exercise.name} on {self.date}"

    def calculate_training_volume(self):
        """Calculate the total training volume for this progress record."""
        return self.total_weight * self.total_repetitions

    def can_be_accessed_by(self, user):
        """
        Check if a user can access this progress record.
        
        Args:
            user: CustomUser instance
            
        Returns:
            bool: True if user can access this progress record
        """
        # Users can access their own progress
        if self.user == user:
            return True
            
        # Trainers can access their clients' progress
        if user.is_trainer and self.user.assigned_trainer == user:
            return True
            
        # Admins can access all progress
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
    routine = models.ForeignKey(Routine, on_delete=models.CASCADE, related_name='progress')
    
    # Progress data
    day = models.PositiveIntegerField(help_text="Day of the routine")
    status = models.CharField(
        max_length=20,
        choices=[
            ('Not Started', 'Not Started'),
            ('In Progress', 'In Progress'),
            ('Completed', 'Completed'),
            ('Skipped', 'Skipped')
        ],
        default='Not Started',
        help_text="Progress status for this day"
    )
    
    # Performance metrics
    completion_time = models.DurationField(null=True, blank=True, help_text="Time taken to complete")
    exercises_completed = models.PositiveIntegerField(default=0, help_text="Number of exercises completed")
    total_exercises = models.PositiveIntegerField(default=0, help_text="Total exercises for this day")
    
    # Notes
    notes = models.TextField(blank=True, help_text="User notes about the session")
    
    updated_at = models.DateTimeField(auto_now=True, help_text="Last update time")

    class Meta:
        verbose_name = "Routine Progress"
        verbose_name_plural = "Routine Progress"
        unique_together = [['user', 'routine', 'day']]
        ordering = ['day']

    def __str__(self):
        return f"{self.user.username} - {self.routine.name} Day {self.day}"

    def can_be_accessed_by(self, user):
        """
        Check if a user can access this progress record.
        
        Args:
            user: CustomUser instance
            
        Returns:
            bool: True if user can access this progress record
        """
        # Users can access their own progress
        if self.user == user:
            return True
            
        # Trainers can access their clients' progress
        if user.is_trainer and self.user.assigned_trainer == user:
            return True
            
        # Admins can access all progress
        if user.is_admin:
            return True
            
        return False

    @property
    def completion_percentage(self):
        """Calculate the completion percentage for this day."""
        if self.total_exercises == 0:
            return 0
        return (self.exercises_completed / self.total_exercises) * 100