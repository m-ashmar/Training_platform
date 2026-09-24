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
from django.db.models.signals import post_delete, post_save, m2m_changed
from django.dispatch import receiver
import contextlib as _contextlib
import threading as _threading
import logging
from training_platform.model_validation import RowValidationMixin

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

class Exercise(RowValidationMixin, models.Model):
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
        # The left half is the stored key and must not move; the right half is what a
        # person reads, so it goes through the catalogue. Serialised as
        # `target_muscle_display`, which stayed English under Accept-Language: ar
        # because these labels were plain strings.
        choices=[
            ("Upper Chest", _("Upper Chest")),
            ("Lower Chest", _("Lower Chest")),
            ("Middle Chest", _("Middle Chest")),
            ("Lateral Deltoid", _("Lateral Deltoid")),
            ("Rear Deltoid", _("Rear Deltoid")),
            ("Front Deltoid", _("Front Deltoid")),
            ("Biceps", _("Biceps")),
            ("Triceps", _("Triceps")),
            ("Forearms", _("Forearms")),
            ("Upper Back", _("Upper Back")),
            ("Lats", _("Lats")),
            ("Lower Back", _("Lower Back")),
            ("Traps", _("Traps")),
            ("Abdominals", _("Abdominals")),
            ("Obliques", _("Obliques")),
            ("Glutes", _("Glutes")),
            ("Front Quads", _("Front Quads")),
            ("Hamstrings", _("Hamstrings")),
            ("Calves", _("Calves")),
            ("Adductors", _("Adductors")),
            ("Abductors", _("Abductors")),
            ("Neck", _("Neck")),
            ("Other", _("Other"))
        ],
        default="Other",
        help_text="Targeted muscle group for this exercise (granular)"
    )
    
    # Exercise metadata
    difficulty_level = models.CharField(
        max_length=20,
        choices=[
            ('beginner', _('Beginner')),
            ('intermediate', _('Intermediate')),
            ('advanced', _('Advanced')),
            ('expert', _('Expert'))
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
        constraints = [
            # "Global" had three definitions that disagreed: this column, and
            # `created_by IS NULL` used by both the viewset queryset and the
            # RoutineExercise serializer. Which one executed decided whether a
            # boundary held. Guarding only "global implies no owner" left the other
            # half open — an exercise with no owner and is_global false belongs to
            # nobody and is visible to nobody, which is how one disappears. Both
            # directions, so the column and the ownership can only ever agree.
            models.CheckConstraint(
                condition=(
                    models.Q(is_global=True, created_by__isnull=True)
                    | models.Q(is_global=False, created_by__isnull=False)
                ),
                name='exercise_visibility_follows_ownership',
            ),
        ]
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['name', 'id']

    def __str__(self):
        return self.name

    row_validation_exclude = ("created_by",)

    def clean(self):
        """Validate exercise data.

        `is_global` is not an independent choice: an exercise is either part of the
        platform catalogue (no owner, visible to everyone) or a trainer's own (owned,
        visible to them). Storing both facts let them disagree, so `save()` derives the
        column from ownership and a constraint holds the pair together. Validation must
        not silently rewrite what the caller passed, so this only reports the conflict.
        """
        if self.created_by and not self.created_by.is_trainer:
            raise ValidationError(_("Only trainers can create exercises"), code="trainer_required")

        if self.is_global != (self.created_by_id is None):
            raise ValidationError(
                {"is_global": _("A global exercise has no owner; an owned exercise is not global.")},
                code="global_owner_conflict",
            )

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
        # A replaced image is NOT removed here. It was: the old file was deleted inline,
        # before `validate_row()` and before the UPDATE, so a save that then failed —
        # or a transaction rolled back later — left the row pointing at a file that no
        # longer existed, and every save cost two extra storage round-trips on object
        # storage (SIDE_BUGS S-109). `training_platform/signals.py`
        # `delete_replaced_files_on_update` is connected to every model with a file
        # field, this one included, and removes the old file only once the write has
        # COMMITTED — which is the whole reason it exists.
        #
        # Visibility follows ownership; it is not a separate decision. Deriving the
        # column here is what stops it and `created_by` from ever disagreeing, which
        # is how the same exercise came to read as global to one code path and
        # private to another.
        self.is_global = self.created_by_id is None
        self.validate_row()
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

    #: Where `exercises/create-with-image/` stores an uploaded photo, as
    #: `exercise_media/<exercise id>/<name>`. A `photo` row whose `content` starts with
    #: this is a STORAGE NAME the platform wrote; any other `content` is a URL a coach
    #: gave (`add-media/` accepts only http(s) URLs) or, for `text`, prose.
    STORED_PHOTO_PREFIX = 'exercise_media/'

    def stored_name(self):
        """The storage name of a photo this platform stored, else None.

        The row used to keep `default_storage.url(name)` instead — a SIGNED url, which
        on object storage expires after `MEDIA_SIGNED_URL_TTL` (an hour), so every photo
        uploaded through `create-with-image/` was a dead link soon after it was written
        (SIDE_BUGS S-070). The name is stored; `content_url` makes the url when served.
        """
        content = self.content or ''
        if self.media_type == 'photo' and content.startswith(self.STORED_PHOTO_PREFIX):
            return content
        return None

    def content_url(self, request=None):
        """`content` as the app should load it: a stored photo is signed NOW, through
        `media_url` (the one function every media URL goes through); a URL or a text is
        returned as it is."""
        name = self.stored_name()
        if name is None:
            return self.content
        from training_platform.media_storage import media_url
        return media_url(name, request)


@receiver(post_delete, sender=ExerciseMedia, dispatch_uid='exercise-media-stored-photo')
def remove_a_stored_exercise_photo(sender, instance, **kwargs):
    """A stored photo goes with its row — after the commit, never before.

    Nothing removed it: the row held a URL, which is not a file, so deleting the media
    (`add-media/` DELETE) or the whole exercise (its media CASCADE) left the bytes in
    storage for ever (SIDE_BUGS S-070). Only a name this platform wrote is touched — a
    coach's URL is somebody else's file. After the commit, like
    `training_platform/signals.py` `_delete_after_commit`, so a rolled-back delete keeps
    a live row pointing at a file that still exists.
    """
    name = instance.stored_name()
    if not name:
        return
    if ExerciseMedia.objects.filter(content=name, media_type='photo').exclude(
            pk=instance.pk).exists():
        return

    def _remove():
        try:
            default_storage.delete(name)
        except Exception:  # noqa: BLE001 — storage may be down; never fail the delete
            logger.warning("could not remove stored exercise photo %s", name,
                           exc_info=True)

    transaction.on_commit(_remove)


class Routine(RowValidationMixin, models.Model):
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
    @staticmethod
    def forget_translations(translations, *fields):
        """`translations` without `fields` in any language: after the name or
        description is edited, the edited column is what every language shows until a
        translation is written again. A stale copy hid the edit from Arabic readers and
        from every notification."""
        if not fields or not isinstance(translations, dict):
            return translations or {}
        kept = {}
        for language, texts in translations.items():
            if isinstance(texts, dict):
                texts = {k: v for k, v in texts.items() if k not in fields}
                if texts:
                    kept[language] = texts
        return kept

    # Which template this private copy was written from, if any. Values are copied, so
    # editing or deleting the template never changes the routine; this only lets the
    # trainer see which clients got which template.
    source_template = models.ForeignKey(
        'RoutineTemplate', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='generated_routines',
        help_text="The template this routine was generated from (a copy, not a link)")
    start_date = models.DateField(default=date.today, help_text="Start date for the routine")
    end_date = models.DateField(blank=True, null=True, db_index=True, help_text="End date for the routine")
    
    # Routine metadata
    difficulty_level = models.CharField(
        max_length=20,
        choices=[
            ('beginner', _('Beginner')),
            ('intermediate', _('Intermediate')),
            ('advanced', _('Advanced')),
            ('expert', _('Expert'))
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
        # The name, and nothing that needs the database. This ran two queries — the first
        # five assignees' usernames and a COUNT — every time a routine was rendered as
        # text: every admin changelist row (90 rows of routine progress cost 180 queries),
        # every FK dropdown option, every admin log entry. It also put up to five client
        # usernames into `LogEntry.object_repr` and anywhere else a routine was printed.
        return self.name

    row_validation_exclude = ("created_by",)

    def clean(self):
        """Validate what this row decides on its own.

        Who may be assigned to a routine is not one of those things. That check used
        to live here and compared each assigned client's *current* trainer against the
        creator, so moving a client to another trainer retroactively invalidated every
        routine the first trainer had ever written for them — 100 rows in this
        database alone. Membership is settled where the link is made, by
        `RoutineSerializer._validate_client_assignments`, against the approved
        trainer-client relation rather than a mutable pointer on the user.
        """
        if not self.created_by.is_trainer:
            raise ValidationError(_("Only trainers can create routines"), code="trainer_required")

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
        self.validate_row()
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


class RoutineExercise(RowValidationMixin, models.Model):
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
        indexes = [
            # `filter(routine=, day=)` runs on every progress read and on every
            # assignment; `unique_together` gives only a `routine` prefix, so a
            # 365-day routine scanned every one of its rows for each day lookup.
            models.Index(fields=['routine', 'day', 'order'], name='routineex_day_idx'),
        ]

    def __str__(self):
        return f"{self.exercise.name} in {self.routine.name} (Day {self.day}, Order {self.order})"

    def clean(self):
        """Validate what this row decides on its own.

        Neither of the rules that used to live here qualified. Both read another row's
        present state: whether the exercise is *still* accessible to the routine's
        creator, and whether the day *still* fits the routine's length. Deactivating an
        exercise or shortening a routine therefore made existing rows unsaveable — 878
        of 2201 here. Both are now checked by `RoutineExerciseSerializer.validate`, at
        the moment a client asks to add or move an exercise.
        """
        if self.day is not None and self.day < 1:
            raise ValidationError({"day": _("Day is 1-based.")}, code="day_invalid")

    def save(self, *args, **kwargs):
        self.validate_row()
        super().save(*args, **kwargs)


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
        
        # Trainer can access their client's progress — through an APPROVED relation.
        # `self.user.assigned_trainer == user` read the denormalised display pointer,
        # which routine/permissions.py forbids as an access test.
        if user.is_trainer:
            from .permissions import can_access_user_data
            if can_access_user_data(user, self.user_id):
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
    #: Named, so a caller can validate against it. `update_progress` passed whatever
    #: string arrived straight into `update_or_create`, which never calls `full_clean`,
    #: so 'banana' was stored and returned with a 200.
    STATUS_CHOICES = [
        # Stored values are lowercase snake_case, matching the convention already
        # used by WorkoutSession, social, subscription and analytics. RoutineProgress
        # was the lone outlier storing Title Case, which forced any API consumer to
        # implement two vocabularies for the same concept.
        ('not_started', 'Not Started'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('skipped', 'Skipped'),
    ]
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
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
        
        # Trainer can access their client's progress — through an APPROVED relation.
        # `self.user.assigned_trainer == user` read the denormalised display pointer,
        # which routine/permissions.py forbids as an access test.
        if user.is_trainer:
            from .permissions import can_access_user_data
            if can_access_user_data(user, self.user_id):
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



class DayOutcome(models.Model):
    """What one training day planned and what was done — written when the day closes.

    The owner (2026-09-20): keep the missed work instead of throwing it away, so the app
    can say "18 of 24 sets", "5 days with nothing missed", and so a day closed short
    breaks the streak instead of passing for a perfect one. See `routine/outcomes.py`,
    which is the only writer; nothing else may edit these rows.

    The plan columns are a snapshot frozen when the day closed, because the coach edits
    `RoutineExercise` in place and an edit must not rewrite last week. The logged columns
    follow later sets of the same date, like "a completed day stays completed".
    """
    VIA_CHOICES = [
        ('completed', 'Closed by the client, nothing missing'),
        ('confirmed_unlogged', 'Closed by the client after the unlogged-sets warning'),
        ('skipped', 'Marked skipped by the client'),
        ('bulk_complete', 'Logged a whole day at once'),
        ('session', 'A workout session was finished on this day'),
        ('auto', 'Every exercise reached its target, so the day completed itself'),
        ('unclosed', 'Opened and never closed; recorded by the nightly sweep'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='day_outcomes')
    #: PROTECT, like `RoutineProgress`: a routine with history is retired, never deleted.
    routine = models.ForeignKey('Routine', on_delete=models.PROTECT,
                                related_name='day_outcomes')
    day = models.PositiveIntegerField(help_text="Day of the routine")
    date = models.DateField(help_text="The calendar day that was trained")

    via = models.CharField(max_length=20, choices=VIA_CHOICES)
    #: The person was warned that sets were unlogged and closed the day anyway.
    confirmed_unlogged = models.BooleanField(default=False)
    #: Nothing missing: every prescribed set of the day is accounted for.
    complete = models.BooleanField(default=False, db_index=True)

    planned_sets = models.PositiveIntegerField(default=0)
    logged_sets = models.PositiveIntegerField(default=0)
    missed_sets = models.PositiveIntegerField(default=0)
    #: Null when the coach prescribed no repetitions for any exercise of the day.
    planned_reps = models.PositiveIntegerField(null=True, blank=True)
    logged_reps = models.PositiveIntegerField(default=0)
    #: False when part of the day came from "log a whole day", which records no reps.
    reps_known = models.BooleanField(default=True)
    #: Null unless the coach prescribed a working weight (and reps) for the day.
    planned_volume_kg = models.FloatField(null=True, blank=True)
    logged_volume_kg = models.FloatField(default=0)

    exercises_planned = models.PositiveIntegerField(default=0)
    exercises_full = models.PositiveIntegerField(default=0)
    exercises_partial = models.PositiveIntegerField(default=0)
    exercises_untouched = models.PositiveIntegerField(default=0)
    exercises_skipped = models.PositiveIntegerField(default=0)

    #: False for a day filled in afterwards by the backfill: the plan may have changed
    #: since, so the week is shown as approximate.
    prescription_exact = models.BooleanField(default=True)
    workout_session = models.ForeignKey('WorkoutSession', null=True, blank=True,
                                        on_delete=models.SET_NULL,
                                        related_name='day_outcomes')
    #: One entry per prescribed exercise: planned and logged sets, reps, weight, skipped.
    detail = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Day Outcome"
        verbose_name_plural = "Day Outcomes"
        constraints = [
            models.UniqueConstraint(fields=['user', 'routine', 'day', 'date'],
                                    name='one_outcome_per_training_day'),
        ]
        indexes = [
            models.Index(fields=['user', '-date'], name='dayoutcome_user_recent_idx'),
        ]
        # `id` tiebreaker: a single non-unique sort column is not a total order, so
        # LIMIT/OFFSET paging repeats and hides rows.
        ordering = ['-date', '-id']

    def __str__(self):
        return f"{self.user_id} {self.routine_id} day {self.day} on {self.date}"


class WorkoutSession(models.Model):
    """
    Represents a user's workout session. Tracks start/end and status for real-time execution.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='workout_sessions')
    routine = models.ForeignKey(Routine, on_delete=models.CASCADE, related_name='workout_sessions')
    #: WHICH day of the routine this session is. Nullable, because a session can be
    #: opened without naming one and older rows have none.
    #:
    #: Nothing recorded it, and `UserExerciseProgress` is keyed `(user, exercise, date)`
    #: with no day either — so when a routine schedules the same exercise on more than
    #: one day, completing it once marked EVERY one of those days done. A full-body
    #: routine reported the whole week complete from a single workout, and the streak
    #: and completion figures were credited for days the client never trained. With the
    #: day known, exactly one day moves.
    day = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Which day of the routine this session covers, when the client said.")
    #: When the workout began. Defaults to the moment the row is written, and the app
    #: may say otherwise on create: a workout logged offline is written when the phone
    #: syncs, and `auto_now_add` stamped that time instead of the real one, so the
    #: 12-hour cap on `end_time` then refused the real finish (BACKEND_BUGS B-003).
    start_time = models.DateTimeField(default=timezone.now)
    end_time = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=[('active', 'Active'), ('completed', 'Completed'), ('abandoned', 'Abandoned')], default='active')
    #: The app's own id for this session, so a retried create returns the first row
    #: instead of writing a second one (B-003). Optional; unique when given.
    client_uuid = models.UUIDField(null=True, blank=True, unique=True)
    #: How hard the workout felt, 1 (easy) to 5 (all I had), as the PERSON WHO TRAINED
    #: judges it (BACKEND_DELTA item 11). Null while unanswered, and it stays answerable
    #: afterwards — a client rates the session on its own screen, often the next morning.
    #: Per-set `ExerciseSetLog.rpe` is 1-10 and about one set; this is about the session.
    #: The check constraint below is the floor: a serializer can be bypassed, a
    #: constraint cannot, and every progression rule divides by these numbers.
    effort_rating = models.PositiveSmallIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="How hard the whole session felt: 1 (easy) to 5 (all I had).")

    class Meta:
        verbose_name = "Workout Session"
        verbose_name_plural = "Workout Sessions"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(effort_rating__isnull=True)
                | models.Q(effort_rating__gte=1, effort_rating__lte=5),
                name='workoutsession_effort_rating_1_to_5'),
        ]
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
            # Every trainer dashboard filters completed sessions and orders by
            # `end_time`; every index above is on `start_time`, so those reads sorted
            # the client's whole history to return the last few.
            models.Index(fields=['user', 'status', '-end_time'], name='workoutsess_ended_idx'),
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


class ExerciseSetLog(RowValidationMixin, models.Model):
    """
    Detailed log of individual sets within an exercise session.

    **The progress row owns the date.** `UserExerciseProgress` is keyed
    `(user, exercise, date)` — that key IS the training day — so a set belongs to the
    day its progress row is on and `ExerciseSetLog.date` only ever repeats it.
    `clean()` refuses a row whose `date` says otherwise (`set_date_mismatch`), because
    the two dates were independent and two different readers believed different ones:
    `routine/outcomes.py` `day_work` counts a set by the LOG's date while
    `routine/models.py` `refresh_routine_progress` measures the day by the PROGRESS
    row's. A set posted with a mismatched date therefore landed on one day in the
    adherence figures and on another in the day's completion — one workout, two
    contradictory histories, and a short day that could be closed by moving a set onto
    it. The column stays for the indexes that read it; it is a copy, not a second
    opinion.

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
    
    #: A COPY of `user_exercise_progress.date`, which is the day this set was trained.
    #: See the class docstring: `clean()` refuses any other value.
    date = models.DateField(default=timezone.localdate, null=True, blank=True, help_text="Date of the set")  # TODO: Backfill and make non-nullable if desired
    #: The app's own id for this set: a retried POST (the write landed, the answer was
    #: lost) returns the first row instead of logging the set twice (B-003).
    client_uuid = models.UUIDField(null=True, blank=True, unique=True)
    #: When the SERVER received this set. Not the day it was trained (`date`): a set may
    #: be logged up to `routine/views.py MAX_BACKLOG_DAYS` late for the person's own
    #: journal, and nothing here changes that. A challenge refuses to COUNT a set that
    #: arrived more than `social/challenge_progress.py LATE_HOURS` after its training
    #: day ended (owner, 2026-09-24, F20). Null on every row written before the column
    #: existed: those count as on time.
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    #: When the set's WEIGHT or REPS last changed after it was first written (null: never
    #: changed). A set logged on time and raised days later kept its `created_at`, so it
    #: slipped past the late rule; a challenge refuses to count a set whose values
    #: changed more than `LATE_HOURS` after its training day ended, as it refuses one
    #: that arrived that late (owner, 2026-09-24, F20). Stamped by `save()` whenever the
    #: two values differ from what was read, `update_fields` or not.
    values_changed_at = models.DateTimeField(null=True, blank=True)

    #: The values a challenge counts; a change to either stamps `values_changed_at`.
    COUNTED_VALUES = ('weight', 'reps')

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
        # One day per set. See the class docstring: the progress row decides, and a
        # writer that disagrees is refused here rather than leaving the platform with
        # two answers for the same workout. Checked on the id, so a row whose progress
        # is not loaded costs one query and a row with no progress row (an old, free
        # log) is left alone.
        if self.user_exercise_progress_id and self.date is not None:
            owner_date = self.user_exercise_progress.date
            if owner_date is not None and owner_date != self.date:
                raise ValidationError(
                    {'date': ValidationError(
                        _("A set is logged on the day of the exercise it belongs to "
                          "(%(day)s). Log it against that day's progress record."),
                        code="set_date_mismatch", params={'day': owner_date})})

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        if all(name in field_names for name in cls.COUNTED_VALUES):
            instance._loaded_values = tuple(getattr(instance, name)
                                            for name in cls.COUNTED_VALUES)
        return instance

    def save(self, *args, **kwargs):
        self.validate_row()
        current = tuple(getattr(self, name) for name in self.COUNTED_VALUES)
        loaded = getattr(self, '_loaded_values', None)
        if self.pk is not None and loaded is not None and loaded != current:
            self.values_changed_at = timezone.now()
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = {*kwargs['update_fields'], 'values_changed_at'}
        super().save(*args, **kwargs)
        self._loaded_values = current



class RoutineTemplate(RowValidationMixin, models.Model):
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

    def save(self, *args, **kwargs):
        self.validate_row()
        super().save(*args, **kwargs)


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

def _session_of_record(progress):
    """The workout session this progress was logged in, if there was one.

    `UserExerciseProgress` carries neither a routine nor a day, but its set logs point
    at a `WorkoutSession`, which knows both. That is the only honest link between "work
    was logged" and "which day of which plan it was for".
    """
    log = (ExerciseSetLog.objects
           .filter(user_exercise_progress=progress, workout_session__isnull=False)
           .select_related('workout_session__routine')
           .order_by('-id')
           .first())
    return getattr(log, 'workout_session', None)


#: The ways a PERSON closes a day as done (`DayOutcome.via`): "complete", "complete
#: anyway" after the unlogged-sets warning, and "log the whole day". A day closed any
#: other way that reads `completed` got there from its sets alone (`auto`, or `session`
#: on top of it), so its status follows them (SIDE_BUGS S-110).
PERSON_CLOSED_VIAS = ('completed', 'confirmed_unlogged', 'bulk_complete')


def _closed_by_person(user, routine, day, date) -> bool:
    """Whether a `completed` day was closed by the person rather than by its own sets.

    A completed day with NO outcome counts as closed by the person: `routine-progress/`
    writes the status with no outcome, and rows from before outcomes existed have none.
    Nobody knows how those were closed, and a status a person set is not undone on a
    guess."""
    via = (DayOutcome.objects.filter(user=user, routine=routine, day=day, date=date)
           .values_list('via', flat=True).first())
    return via is None or via in PERSON_CLOSED_VIAS


@receiver(post_save, sender=UserExerciseProgress)
def update_routine_progress_on_exercise_progress(sender, instance, created, **kwargs):
    """Signal wrapper. The work is in `refresh_routine_progress` so that callers which
    write with a queryset `.update()` — which emits no `post_save` — can run it too."""
    if getattr(_recalc_state, 'suspended', False) and not kwargs.get('_forced'):
        return
    refresh_routine_progress(instance)


def refresh_routine_progress(instance, routine_scope=None, day_scope=None):
    """Write `RoutineProgress` for the work this `UserExerciseProgress` represents.

    `routine_scope`, when given, is the routine the client was actually training. It
    matters because `UserExerciseProgress` is keyed `(user, exercise, date)` and records
    NO routine and no day, so without a scope this walks every active routine assigned
    to the user that contains the exercise and writes a verdict for every day of each.
    One full-body workout therefore marked the whole week done — and days in OTHER
    routines the client has never opened came back `completed`, because those days
    happened to hold only that exercise. Measured at 20 assigned routines: one
    `bulk-complete` wrote 90 RoutineProgress rows and cost 524 queries.

    The set logs know the routine: `ExerciseSetLog.workout_session.routine`. When they
    do, only that routine is touched. Without a scope — ad-hoc logging with no session —
    a day is only ever marked `completed`, never `in_progress`, because "some exercise
    of this day has progress today" is not evidence the client opened that day at all.
    """
    user = instance.user
    exercise = instance.exercise
    progress_date = instance.date

    session = _session_of_record(instance)
    if routine_scope is None:
        routine_scope = getattr(session, 'routine', None)
    if day_scope is None:
        # A caller that knows the day says so (`bulk-complete`, BACKEND_BUGS B-011);
        # otherwise the session of record does.
        day_scope = getattr(session, 'day', None)

    # Pre-fetch the related routine_exercises to avoid N+1 down the line
    routines = Routine.objects.filter(
        assigned_to=user, 
        routine_exercises__exercise=exercise,
        is_active=True
    ).prefetch_related('routine_exercises', 'routine_exercises__exercise').distinct()
    if routine_scope is not None:
        routines = routines.filter(pk=routine_scope.pk)
    
    for routine in routines:
        # All routine exercises are loaded in memory now
        exercises_in_routine = list(routine.routine_exercises.all())
        
        # Find which days this exercise appears in this routine
        days_with_exercise = {rex.day for rex in exercises_in_routine if rex.exercise_id == exercise.id}
        # The day the client said they were training, when they said. Without it, a
        # routine that schedules one exercise on several days had every one of those
        # days marked from a single workout.
        if day_scope is not None:
            days_with_exercise &= {day_scope}
        
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

            # A day already CLOSED follows its new sets, whatever verdict this pass
            # reaches: that is how a day closed short becomes complete when the rest of
            # it syncs. Asked only for days this person has work on, and it does nothing
            # unless an outcome exists. A day this pass finds COMPLETE is recorded at the
            # end of the loop instead — refreshing it here as well wrote the same row
            # twice on every completing set.
            if progress_by_exercise and status != 'completed':
                from .outcomes import refresh_day_quietly
                refresh_day_quietly(user, routine, day, progress_date)

            # Is this day already recorded as completed? Only asked when this pass
            # finds it is not, which is the case a deleted set creates.
            was_completed = status != 'completed' and RoutineProgress.objects.filter(
                user=user, routine=routine, day=day, date=progress_date,
                status='completed').exists()

            # Without a routine of record we cannot know the client opened THIS day, so
            # a partial count is not evidence of anything: a shared exercise made an
            # untrained day report `in_progress`. A fully-completed day is different —
            # every exercise it schedules was trained today, which is the verdict. A day
            # ALREADY closed is refreshed above either way: it is evidence of itself,
            # and so is a day already recorded as completed (see below).
            if routine_scope is None and status != 'completed' and not was_completed:
                continue

            # A day the PERSON completed stays completed: completing it with sets still
            # unlogged is allowed after a warning (owner, BACKEND_BUGS B-011), and a set
            # logged or deleted after that must not undo it. A day that completed
            # ITSELF from its sets does not: nobody closed it, so when those sets are
            # deleted its status follows them (SIDE_BUGS S-110) — it read `completed`
            # for ever in analytics/completion, the console's completion rate and
            # `routine-progress/`, while its own outcome said it was short.
            if was_completed and _closed_by_person(user, routine, day, progress_date):
                continue

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

            # A day can also complete itself here, with no call to update_progress and
            # no warning: every exercise reached its target. Its outcome is kept like
            # any other closed day (the owner, 2026-09-20), so the week's figures do not
            # depend on which button finished the day.
            if status == 'completed':
                from .outcomes import record_day_quietly
                record_day_quietly(user, routine, day, progress_date, via='auto')

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


def recalc_progress_for(progress, routine_scope=None, day_scope=None):
    """Run the recomputation explicitly (used after a suspended bulk write).

    `routine_scope` and `day_scope` name the routine and day the caller was training.
    Without them the recomputation walks every routine holding the exercise, which is
    what let one workout decide another routine's day (see `refresh_routine_progress`).
    """
    if not progress.set_logs.exists():
        return
    update_routine_progress_on_set_log(
        sender=ExerciseSetLog, instance=progress.set_logs.first(), created=False,
        _forced=True, routine_scope=routine_scope, day_scope=day_scope)


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
    routine_scope = kwargs.get('routine_scope')
    day_scope = kwargs.get('day_scope')
        
    # Defer to after commit. post_save fires INSIDE the insert's transaction, so the
    # aggregate below could not see rows other writers had not yet committed — the row
    # lock alone still produced 400/450/300 where 600 was correct. Running after commit
    # means every recalc reads committed data, and the last one to take the lock sees
    # all of them.
    transaction.on_commit(lambda pk=progress.pk,
                          routine_pk=getattr(routine_scope, 'pk', None), day=day_scope:
                          _recalc_locked(pk, routine_pk, day))


@receiver(post_delete, sender=ExerciseSetLog)
def update_routine_progress_on_set_delete(sender, instance, **kwargs):
    """A deleted set is subtracted from everything the created one added.

    Nothing recomputed on delete before this (BACKEND_DELTA item 15), so removing a set
    left `UserExerciseProgress.completed_sets`, `RoutineProgress` and the `DayOutcome`
    claiming work that no longer exists: a day could stay "nothing missed" — and keep a
    streak alive — on sets the client had deleted.

    `_recalc_locked`, not `recalc_progress_for`: that helper returns early when the
    progress row has no set logs left, which is exactly the case deleting the last set
    of an exercise creates, and the stale totals would survive. `_recalc_locked` writes
    the aggregate (zero, when nothing is left) and then propagates through
    `refresh_routine_progress`, which refreshes any outcome the day already has.

    The routine and the day come from the deleted row's own session when it had one:
    without that scope the propagation walks every routine holding the exercise, which
    is what B-011 was about. A cascade (the progress row, the session or the exercise
    being deleted) leaves nothing to recompute; `_recalc_locked` updates zero rows and
    returns.
    """
    if getattr(_recalc_state, 'suspended', False):
        return
    progress_pk = instance.user_exercise_progress_id
    if not progress_pk:
        return
    # The ids only: touching `instance.workout_session` re-queries a row that may have
    # been deleted in the same cascade, and this receiver must not raise into a delete.
    routine_pk = day_scope = None
    if instance.workout_session_id:
        session = (WorkoutSession.objects.filter(pk=instance.workout_session_id)
                   .values('routine_id', 'day').first())
        if session is not None:
            routine_pk, day_scope = session['routine_id'], session['day']
    transaction.on_commit(lambda pk=progress_pk, routine=routine_pk, day=day_scope:
                          _recalc_locked(pk, routine, day))


def _recalc_locked(progress_pk, routine_pk=None, day_scope=None):
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
    from django.db.models import Count, OuterRef, Q, Subquery, Sum, Value
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
        # DISTINCT set numbers, not rows: only `client_uuid` is unique, so a set
        # re-sent without the app's id writes a second row, and counting rows let three
        # rows for two sets satisfy a three-set prescription — the day then completed
        # itself on work that was never done. A set with no number (an old row, a free
        # log) has no identity to collapse, so each one counts once.
        completed_sets=agg(Count('set_number', distinct=True)
                           + Count('id', filter=Q(set_number__isnull=True)),
                           models.IntegerField()),
        total_weight=agg(Sum('weight'), models.FloatField()),
        total_repetitions=agg(Sum('reps'), models.IntegerField()),
    )

    # And PROPAGATE. A queryset `.update()` emits no `post_save`, so
    # `update_routine_progress_on_exercise_progress` — the receiver that actually writes
    # `RoutineProgress` — never fired: a client could log every set of a workout and the
    # day stayed "not started", the streak stayed 0, and the trainer's completion screen
    # read 0%. The race fix that introduced this UPDATE silently cut the propagation
    # chain the calling function is named after. Only `bulk-complete`, which writes
    # through `save()`, moved the number.
    refreshed = UserExerciseProgress.objects.filter(pk=progress_pk).first()
    if refreshed is not None:
        routine_scope = Routine.objects.filter(pk=routine_pk).first() if routine_pk else None
        refresh_routine_progress(refreshed, routine_scope=routine_scope, day_scope=day_scope)


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


