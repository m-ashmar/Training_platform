from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from datetime import timedelta

from django.db import models
from django.dispatch import receiver
from django.db.models import Q
from django.utils import timezone
import logging
from datetime import date, timedelta
from django.conf import settings
import os
import uuid
from training_platform.encrypted_fields import EncryptedCodeListField, EncryptedTextField
from django.core.validators import MaxValueValidator, MinValueValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

class CustomUserManager(BaseUserManager):
    """
    Custom user manager for creating users and superusers.
    Supports both regular users and trainer accounts.
    """
    def create_user(self, email, username, password=None, phone_number=None, **extra_fields):
        """
        Create and save a regular user or trainer.
        
        Args:
            email: User's email address
            username: Unique username
            password: User password
            phone_number: User's phone number
            **extra_fields: Additional fields including user_type
            
        Returns:
            CustomUser instance
        """
        if not email:
            raise ValueError("The Email field must be set")
        
        extra_fields.setdefault('is_active', True)
        # Accounts made here are made by the platform — createsuperuser, seed commands,
        # staff tooling — so the operator vouches for the address. Self-service signup
        # never comes through the manager (allauth instantiates the model and
        # CustomRegisterSerializer writes `email_verified=False` itself), and signing in
        # now requires a verified address, so without this every operator-created
        # account would be locked out of the API.
        extra_fields.setdefault('email_verified', True)
        email = self.normalize_email(email)
        # phone_number is NOT NULL with a model-level default. Passing None here
        # (the signature default) overrode that default and raised NotNullViolation,
        # so any programmatic create_user() without an explicit phone_number failed.
        if phone_number is None:
            phone_number = self.model._meta.get_field('phone_number').get_default()
        user = self.model(email=email, username=username, phone_number=phone_number, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, username, password=None, phone_number=None, **extra_fields):
        """
        Create and save a superuser with all permissions.
        """
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('user_type', 'admin')  # Superusers are admins

        return self.create_user(email, username, password, phone_number, **extra_fields)

    def create_trainer(self, email, username, password=None, phone_number=None, **extra_fields):
        """
        Create and save a trainer account with appropriate defaults.
        
        Args:
            email: Trainer's email address
            username: Unique username
            password: Trainer password
            phone_number: Trainer's phone number
            **extra_fields: Additional trainer-specific fields
            
        Returns:
            CustomUser instance with trainer type
        """
        extra_fields.setdefault('user_type', 'trainer')
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('is_staff', False)  # Trainers are not staff by default
        
        return self.create_user(email, username, password, phone_number, **extra_fields)

def user_profile_picture_upload_path(instance, filename):
    ext = filename.split('.')[-1]
    # Use uuid4 for unique filenames
    filename = f"user_{instance.pk or 'new'}_{uuid.uuid4().hex[:8]}.{ext}"
    return os.path.join('profile_pictures', filename)


def _validate_timezone(value):
    """Reject a timezone name the standard library cannot resolve."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(str(value))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise DjangoValidationError(
            _("%(value)s is not a known timezone."), params={"value": value})


def _default_timezone():
    """Re-read TIME_ZONE per row instead of freezing it at import time."""
    return getattr(settings, 'TIME_ZONE', 'UTC')


#: The only values `calculate_daily_calories` knows how to weight. Declared as
#: `choices`, which DRF enforces and nothing else did — a management command, a fixture
#: or a probe could store 'moderate', and one such row was enough to raise out of a
#: serializer method and turn an admin's whole client list into a 500. Migration 0031
#: makes the database refuse it. Module-level so `Meta.constraints` can name it too.
ACTIVITY_LEVELS = [
    ('Sedentary', 'Sedentary'),
    ('Light', 'Light Exercise'),
    ('Moderate', 'Moderate Exercise'),
    ('Active', 'Active'),
    ('VeryActive', 'Very Active'),
]


#: The onboarding vocabulary (design v2: OnbGoal, OnbActivity, OnbHealth). Codes are the
#: contract: the app draws its own labels for them, and they never change once shipped.
TRAINING_GOALS = [
    ('build_muscle', 'Build muscle'),
    ('fat_loss', 'Lose fat'),
    ('strength', 'Strength'),
    ('general_fitness', 'General fitness'),
]
TRAINING_PLACES = [
    ('gym', 'Gym'),
    ('home', 'Home'),
]
#: Where it hurts. "None" is an empty list, not a code; 'other' is a health condition
#: the client describes in `specific_injury`.
INJURY_AREAS = [
    ('shoulder', 'Shoulder'),
    ('lower_back', 'Lower back'),
    ('knee', 'Knee'),
    ('neck', 'Neck'),
    ('wrist', 'Wrist'),
    ('ankle', 'Ankle'),
    ('hip', 'Hip'),
    ('other', 'Other health condition'),
]
#: What a trainer coaches: the client goals, plus nutrition (a roster filter).
TRAINER_FOCUS = TRAINING_GOALS + [('nutrition', 'Nutrition')]


def _validate_codes(value, choices):
    """A list of codes drawn from `choices`, each at most once."""
    allowed = {c for c, _label in choices}
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise DjangoValidationError(_("Send a list of codes."), code="invalid")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise DjangoValidationError(_("Unknown codes: %(codes)s."),
                                    params={"codes": ", ".join(unknown)}, code="invalid_choice")
    if len(set(value)) != len(value):
        raise DjangoValidationError(_("A code is listed twice."), code="duplicate")


# Module-level, so migrations can name them.
def validate_injury_areas(value):
    _validate_codes(value, INJURY_AREAS)


def validate_trainer_focus(value):
    _validate_codes(value, TRAINER_FOCUS)


def validate_training_places(value):
    _validate_codes(value, TRAINING_PLACES)


def age_on(born, today=None):
    """Whole years from `born` to `today` (default: today). Feb 29 turns a year older
    on Mar 1 in a year without one."""
    today = today or timezone.localdate()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def minimum_age():
    """The youngest a person may be to hold an account (owner's decision: 14)."""
    return int(getattr(settings, 'MINIMUM_AGE_YEARS', 14))


class CustomUser(AbstractBaseUser, PermissionsMixin):
    """
    Enhanced CustomUser model supporting multi-trainer functionality.
    
    This model supports three user types:
    - 'client': Regular users who can be assigned to trainers
    - 'trainer': Users who can manage clients and create content
    - 'admin': Superusers with full system access
    
    TODO: Consider implementing soft delete for user accounts
    TODO: Add audit logging for user type changes
    TODO: Implement user activity tracking
    TODO: Add support for user profile pictures
    TODO: Consider implementing user verification system
    """
    
    # Core authentication fields
    username = models.CharField(max_length=150, unique=True)
    email = models.EmailField(unique=True)
    date_joined = models.DateTimeField(default=timezone.now)
    phone_number = models.CharField(max_length=15, default="0000000000", null=False, blank=True, db_index=True)
    
    # Basic profile fields
    first_name = models.CharField(max_length=30, blank=True)
    last_name = models.CharField(max_length=30, blank=True)

    # Profile picture
    # Opt-in for retaining chat + health context to train models. Defaults to False:
    # the snapshot includes injury/condition text, which is special-category data.
    ai_training_consent = models.BooleanField(
        default=False,
        help_text="User consented to their AI chat data being retained for model training.",
    )
    # When the person last answered that question themselves, through
    # `POST /api/ai/training-consent/` — the only writer. NULL means they were never
    # asked, which is how the app tells "not asked yet" from "said no", as
    # `moderation.AIConsent` keeps `granted_at` / `revoked_at` for the other AI consent.
    ai_training_consent_set_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the user last set ai_training_consent themselves (null: never).",
    )
    profile_picture = models.ImageField(
        upload_to=user_profile_picture_upload_path,
        blank=True,
        null=True,
        help_text="User's profile picture"
    )
    
    # User type and permissions
    USER_TYPE_CHOICES = [
        ('client', 'Client'),
        ('trainer', 'Trainer'),
        ('agent', 'Agent'),
        ('admin', 'Administrator'),
    ]
    user_type = models.CharField(
        max_length=10,
        choices=USER_TYPE_CHOICES,
        default='client',
        help_text="Determines user permissions and access levels"
    )
    
    # Django auth fields
    is_active = models.BooleanField(default=True, db_index=True)
    #: Has this person proved they own the address they signed up with?
    #:
    #: `is_active` was carrying two unrelated meanings — "email verified" and "account
    #: enabled" — and the registration OTP flow owned the write. A moderator suspending
    #: an account therefore put it into exactly the state the resend-OTP endpoint exists
    #: to service: `ResendOTPView` gated on `if user.is_active`, sent a fresh
    #: registration code to the suspended person's own inbox, and `verify-otp` set
    #: `is_active = True` and handed them a token. Suspension undid itself.
    #:
    #: Splitting the two means a suspended account is `is_active=False,
    #: email_verified=True`, which the resend endpoint declines and verify-otp cannot
    #: re-enable. `default=False` and a migration that back-fills every already-enabled
    #: account, so nobody has to re-verify.
    #:
    #: The split was only half done. Registration still wrote `is_active=False` to mean
    #: "not verified yet", so a pending signup and a suspended account both read
    #: `is_active=False` and the resend endpoint, unable to tell them apart, refused
    #: both. Measured: register 201, the 10-minute code expires (or the first email
    #: never leaves), resend answers 200 "a new OTP has been sent" and sends nothing,
    #: and the address can never be verified, logged into, reset or registered again.
    #: Each column now has one meaning and one set of writers:
    #:
    #:     is_active  email_verified
    #:     True       False           pending signup: may request and enter a code,
    #:                                gets no credential until it does
    #:     True       True            a working account
    #:     False      either          disabled (suspended, erased, a ledger owner):
    #:                                no code, no credential, and verifying never
    #:                                re-enables it
    #:
    #: `is_active` is written by staff and by erasure; `email_verified` by verify-otp.
    #: Anything that lists people to other people (trainer directories) asks for both.
    email_verified = models.BooleanField(
        default=False, db_index=True,
        help_text="The address was confirmed by OTP. Separate from is_active, which is "
                  "whether the account is allowed to be used at all.")
    is_staff = models.BooleanField(default=False)
    
    # User Preferences
    preferred_language = models.CharField(
        max_length=10, 
        choices=settings.LANGUAGES, 
        default=settings.LANGUAGE_CODE,
        help_text="User's preferred language for notifications and API responses"
    )
    # `default=getattr(settings, ...)` was evaluated once at import, so the column
    # default froze to whatever TIME_ZONE happened to be when the class was first
    # loaded rather than tracking the setting. A callable is re-evaluated per row.
    preferred_timezone = models.CharField(
        max_length=50,
        default=_default_timezone,
        # Validated, because the failure is silent. Any string was accepted here, and
        # `notifications/tasks.py` catches ZoneInfoNotFoundError and falls back to UTC —
        # so a typo left the user receiving reminders in the wrong part of their day with
        # nothing anywhere to say why.
        validators=[_validate_timezone],
        help_text="User's preferred timezone. Used to decide when scheduled "
                  "notifications fire in the user's own day."
    )
    # Local hour (0-23) at which the workout reminder is sent. Until this existed the
    # platform held no time-of-day anywhere — not on Routine, not on WorkoutSession —
    # so a reminder could only ever be "you have not trained in N days", never
    # "your session is today". 18:00 is the default because it is the hour before a
    # typical evening training slot, not after it.
    workout_reminder_hour = models.PositiveSmallIntegerField(
        default=18,
        validators=[MinValueValidator(0), MaxValueValidator(23)],
        help_text="Local hour (0-23) to send the daily workout reminder."
    )
    
    # Physical attributes (migrated from old fields - preserved for backward compatibility)
    # Bounded. Nothing rejected a height of 1.75 — metres, entered where centimetres
    # were expected — and Mifflin-St Jeor on it produced a BMR of about 560, which the
    # floor silently turned into a 1,200 kcal plan. The whole physiology layer trusted
    # three unvalidated numbers.
    height = models.FloatField(null=True, blank=True, help_text="Height in cm",
                               validators=[MinValueValidator(100.0), MaxValueValidator(250.0)])
    weight = models.FloatField(null=True, blank=True, help_text="Weight in kg",
                               validators=[MinValueValidator(30.0), MaxValueValidator(300.0)])
    age = models.PositiveIntegerField(null=True, blank=True,
                                      validators=[MinValueValidator(13), MaxValueValidator(100)])
    gender = models.CharField(
        max_length=10, 
        choices=[('Male', 'Male'), ('Female', 'Female')], 
        null=True, 
        blank=True
    )
    # Encrypted at rest: free-text medical data, a GDPR special category. Nothing
    # filters or searches this column, which is what makes encryption safe here —
    # ciphertext is randomised, so lookups against it would match nothing.
    specific_injury = EncryptedTextField(null=True, blank=True, help_text="Any injuries or health conditions")
    
    # Activity and fitness data
    activity_level = models.CharField(
        max_length=20,
        choices=ACTIVITY_LEVELS,
        default='Sedentary'
    )
    #: When the person chose `activity_level` through the API. The level has a default,
    #: so without this "never asked" and "Sedentary" could not be told apart
    #: (BACKEND_BUGS B-027); `onboarding_missing` lists it until this is set.
    activity_level_answered_at = models.DateTimeField(null=True, blank=True)

    #: Asked at sign-up; the server refuses anyone younger than `minimum_age()`. While
    #: it is set, `age` is derived from it on every save and refreshed each night
    #: (`users.refresh_ages_from_birth_dates`), because the diet engine and every
    #: existing reader use `age` and must not see a number frozen at sign-up.
    date_of_birth = models.DateField(null=True, blank=True)
    # Onboarding (design v2). Empty means "not answered yet".
    training_goal = models.CharField(max_length=20, choices=TRAINING_GOALS, blank=True,
                                     default='')
    training_place = models.CharField(max_length=10, choices=TRAINING_PLACES, blank=True,
                                      default='')
    training_days_per_week = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(7)])
    #: Structured health data, so encrypted like `specific_injury`. Shown to the client
    #: and their approved trainer only; the matcher reads it to find trainers who work
    #: with these injuries.
    injury_areas = EncryptedCodeListField(default=list, blank=True,
                                          validators=[validate_injury_areas])

    # Trainer-specific fields
    trainer_bio = models.TextField(blank=True, null=True, help_text="Trainer's professional bio")
    trainer_specializations = models.JSONField(
        default=list, 
        blank=True,
        help_text="List of trainer specializations (e.g., ['Strength Training', 'Cardio'])"
    )
    trainer_certifications = models.JSONField(
        default=list, 
        blank=True,
        help_text="List of trainer certifications"
    )
    trainer_experience_years = models.PositiveIntegerField(
        null=True, 
        blank=True,
        help_text="Years of training experience"
    )
    trainer_hourly_rate = models.DecimalField(
        max_digits=8, 
        decimal_places=2, 
        null=True, 
        blank=True,
        help_text="Hourly rate in local currency"
    )
    trainer_is_verified = models.BooleanField(
        default=False,
        help_text="Whether trainer has been verified by admin"
    )
    trainer_is_available = models.BooleanField(
        default=True,
        help_text="Whether trainer is currently accepting new clients"
    )
    # What the trainer coaches, as codes the matcher can compare with a client's
    # onboarding answers. `trainer_specializations` stays the free text shown on cards.
    trainer_goal_focus = models.JSONField(default=list, blank=True,
                                          validators=[validate_trainer_focus])
    trainer_injury_focus = models.JSONField(default=list, blank=True,
                                            validators=[validate_injury_areas])
    trainer_training_places = models.JSONField(default=list, blank=True,
                                               validators=[validate_training_places])

    # Client-specific fields
    assigned_trainer = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='clients',
        help_text="Trainer assigned to this client",
        limit_choices_to={'user_type': 'trainer'}
    )
    client_goals = models.JSONField(
        default=list,
        blank=True,
        help_text="Client's fitness goals (e.g., ['Weight Loss', 'Muscle Gain'])"
    )
    client_preferences = models.JSONField(
        default=dict,
        blank=True,
        help_text="Client's training preferences"
    )
    
    # System fields
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_login = models.DateTimeField(null=True, blank=True)
    
    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username', 'phone_number']

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']
        verbose_name = "User"
        verbose_name_plural = "Users"
        indexes = [
            models.Index(fields=['user_type']),
            models.Index(fields=['email']),
            models.Index(fields=['username']),
            models.Index(fields=['assigned_trainer']),
        ]
        constraints = [
            # `choices` is a form-layer rule. Everything that writes this column
            # without going through a serializer bypassed it, and
            # `calculate_daily_calories` raises on a value it does not recognise, so
            # one such row failed a whole list response for everybody in it.
            models.CheckConstraint(
                condition=models.Q(activity_level__in=[c[0] for c in ACTIVITY_LEVELS]),
                name='user_activity_level_is_a_declared_choice',
            ),
        ]

    def __str__(self):
        return f"{self.username} ({self.get_user_type_display()})"

    @property
    def is_trainer(self):
        """Check if user is a trainer."""
        return self.user_type == 'trainer'

    @property
    def is_client(self):
        """Check if user is a client."""
        return self.user_type == 'client'

    @property
    def is_admin(self):
        """Check if user is an admin."""
        return self.user_type == 'admin'

    @property
    def is_agent(self):
        """Check if user is an agent."""
        return self.user_type == 'agent'

    @property
    def is_onboarding_completed(self):
        """
        Check if user has completed onboarding (filled basic profile).
        
        Criteria:
        - All users: First Name, Last Name
        - Clients: Height, Weight, Age, Gender
        """
        basic_info = bool(self.first_name and self.last_name)
        if not basic_info:
            return False
            
        if self.is_client:
            return all([self.height, self.weight, self.age, self.gender])
            
        return True

    @property
    def full_name(self):
        """Get user's full name."""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.username

    def get_client_count(self):
        """Get number of clients assigned to this trainer."""
        if not self.is_trainer:
            return 0
        return self.clients.count()

    def get_active_routines_count(self):
        """Get number of active routines for this user."""
        if self.is_client:
            return self.assigned_routines.filter(is_active=True).count()
        elif self.is_trainer:
            return self.created_routines.filter(is_active=True).count()
        return 0

    def calculate_bmi(self):
        """Calculate BMI if height and weight are available."""
        if self.height and self.weight:
            return round(self.weight / ((self.height/100) ** 2), 2)
        return None

    @property
    def activity_level_unanswered(self):
        """True until the person chose an activity level. It has a default (Sedentary),
        so the value alone cannot say; a row from before the stamp existed counts as
        answered when it differs from the default (BACKEND_BUGS B-027). A plan is not
        generated from an unanswered level (the owner, 2026-09-20): it would silently
        use the sedentary factor."""
        return self.activity_level_answered_at is None and self.activity_level == 'Sedentary'

    def calculate_bmr(self):
        """Calculate Basal Metabolic Rate using Mifflin-St Jeor Equation."""
        if self.gender and self.height and self.weight and self.age:
            if self.gender == 'Male':
                return 10 * self.weight + 6.25 * self.height - 5 * self.age + 5
            return 10 * self.weight + 6.25 * self.height - 5 * self.age - 161
        return None

    # Widely used clinical floors for a medically unsupervised diet, and a ceiling
    # comfortably above any real athlete. A plan outside these is not a plan.
    MIN_DAILY_CALORIES = 1200
    MAX_DAILY_CALORIES = 6000

    #: The vocabulary `DietPlan.goal` stores. The diet planner works in lower case
    #: internally; it lower-cases at its own boundary rather than storing a second set.
    FITNESS_GOALS = ('Lose', 'Maintain', 'Gain')

    _GOAL_LOSE_WORDS = ('lose', 'loss', 'fat', 'cut', 'slim', 'lean')
    _GOAL_GAIN_WORDS = ('gain', 'muscle', 'bulk', 'mass', 'strength')
    #: The same reading of the onboarding codes the word lists give their free text.
    _TRAINING_GOAL_DIRECTION = {'build_muscle': 'Gain', 'strength': 'Gain',
                                'fat_loss': 'Lose', 'general_fitness': 'Maintain'}

    def resolve_fitness_goal(self):
        """This user's goal, as one of `FITNESS_GOALS`.

        `fitness_goal` is read in five places across the diet app and is a field on no
        model, so every `getattr(user, 'fitness_goal', 'Maintain')` returned its
        fallback. The goals are in `client_goals`; read them from one place.

        A profile asking for both directions at once gets Maintain. Guessing between a
        deficit and a surplus is worse than declining to guess.

        The structured onboarding answer, `training_goal`, wins when it is set: it is a
        single code the client chose, where `client_goals` is free text.
        """
        if self.training_goal:
            return self._TRAINING_GOAL_DIRECTION.get(self.training_goal, 'Maintain')
        words = ' '.join(str(g) for g in (self.client_goals or [])).lower()
        wants_loss = any(w in words for w in self._GOAL_LOSE_WORDS)
        wants_gain = any(w in words for w in self._GOAL_GAIN_WORDS)
        if wants_loss and not wants_gain:
            return 'Lose'
        if wants_gain and not wants_loss:
            return 'Gain'
        return 'Maintain'

    def calculate_daily_calories(self, goal=None):
        """
        Calculate daily calorie needs based on BMR and activity level.

        Args:
            goal: 'Lose', 'Gain', or 'Maintain'. Omit it to use the client's own goal.
                  It used to default to 'Maintain', and every caller that omitted it —
                  plan generation included — silently produced a maintenance target for
                  a client who had asked to lose or gain.

        Returns:
            Daily calorie target or None if insufficient data
        """
        # Any spelling. The comparison below was exact, and the trainer path passed the
        # raw string it was given, so 'lose' or 'Weight Loss' silently produced a
        # MAINTENANCE target on a plan labelled Lose — the same silent-default failure
        # the activity guard eight lines down was written to eliminate.
        goal = self.normalise_goal(goal if goal is not None else self.resolve_fitness_goal())
        # Truthiness, to match calculate_bmr. This guarded `is None` while the BMR
        # guarded truthiness, so a height of 0 passed here, BMR returned None, and the
        # generate view coerced that None into a 0 kcal plan.
        if not (self.height and self.weight and self.age and self.gender):
            raise ValueError("Complete profile required: height, weight, age, gender")

        bmr = self.calculate_bmr()
        if bmr is None:
            return None
            
        activity_factors = {
            'Sedentary': 1.2,
            'Light': 1.375,
            'Moderate': 1.55,
            'Active': 1.725,
            'VeryActive': 1.9
        }
        # An unrecognised activity level is a data error, not a person who never moves.
        # `.get(..., 1.2)` silently treated one as the other: a user stored as 'moderate'
        # instead of 'Moderate' was quietly downgraded to the sedentary factor and told
        # to eat 22.6% less than their real maintenance, with nothing logged anywhere.
        if self.activity_level not in activity_factors:
            raise ValueError(
                f"Unrecognised activity level {self.activity_level!r}. "
                f"Expected one of: {', '.join(activity_factors)}"
            )
        maintenance = bmr * activity_factors[self.activity_level]

        # A sized adjustment, not a flat 500. Twenty percent of maintenance, bounded,
        # and never below BMR: a flat -500 on a small sedentary person was a third of
        # their intake and a flat +500 on a large active one was a rounding error.
        # BMI feeds a decision here for the first time — no deficit below 18.5 — and
        # nobody under 18 is put in a deficit at all.
        bmi = self.calculate_bmi()
        if goal == 'Lose':
            if (bmi is not None and bmi < self.MIN_BMI_FOR_DEFICIT) or (self.age or 0) < 18:
                target = maintenance
            else:
                target = maintenance - max(300.0, min(750.0, 0.20 * maintenance))
                target = max(target, bmr)
        elif goal == 'Gain':
            target = maintenance + max(250.0, min(500.0, 0.15 * maintenance))
        else:
            target = maintenance

        # Clamp. Health bounds, not input validation, so they belong with the calculation.
        return max(self.MIN_DAILY_CALORIES, min(target, self.MAX_DAILY_CALORIES))

    #: Below this a deficit is refused whatever the goal says.
    MIN_BMI_FOR_DEFICIT = 18.5

    @classmethod
    def normalise_goal(cls, value) -> str:
        """'lose', 'Weight Loss', 'cut', 'shred' -> 'Lose'; the gain words -> 'Gain';
        anything else -> 'Maintain'. One resolver for a caller-supplied string."""
        words = str(value or '').lower()
        loss = any(w in words for w in cls._GOAL_LOSE_WORDS)
        gain = any(w in words for w in cls._GOAL_GAIN_WORDS)
        if loss and not gain:
            return 'Lose'
        if gain and not loss:
            return 'Gain'
        return 'Maintain'

    def can_access_user_data(self, target_user):
        """
        Check if this user can access another user's data.
        
        Args:
            target_user: CustomUser instance to check access for
            
        Returns:
            bool: True if access is allowed
        """
        # Admins can access all data
        if self.is_admin:
            return True
            
        # Trainers can access their clients' data.
        #
        # SOURCE OF TRUTH: the approved TrainerClientRelation, not the
        # `assigned_trainer` pointer. The relation is the record that carries consent
        # (who asked, who agreed), the price both sides agreed, and the escrow hold;
        # the pointer is a denormalised convenience for reading "who is my trainer"
        # in one query. Two records meaning the same thing drift, and the half that
        # drifts is the one granting access to another person's body metrics — so
        # only one of them decides, and it is the one with the meaning.
        if self.is_trainer and target_user.is_client:
            return TrainerClientRelation.objects.filter(
                trainer=self, client=target_user, status='approved').exists()
            
        # Users can access their own data
        if target_user == self:
            return True
            
        return False

    @property
    def current_trainer(self):
        """This client's trainer, from the relation that grants it.

        `assigned_trainer` is kept in step by the three places that write it, and is
        what the app reads for display. This is what authorization reads.
        """
        if not self.is_client:
            return None
        relation = (TrainerClientRelation.objects
                    .select_related('trainer')
                    .filter(client=self, status='approved').first())
        return relation.trainer if relation else None

    def get_accessible_clients(self):
        """
        Get all clients this user can access.
        
        Returns:
            QuerySet of accessible clients
        """
        if self.is_admin:
            return CustomUser.objects.filter(user_type='client')
        elif self.is_trainer:
            return self.clients.all()
        return CustomUser.objects.none()

    def retire(self, reason=""):
        """Retire this account without destroying any financial record.

        Delegates to the personal-data registry so there is ONE definition of what
        erasure means. Duplicating the anonymisation here is how the two drift: a field
        added to the registry would keep leaking through this path.

        Wallets, transactions, payments and the audit chain are preserved — those
        sources are registered `on_erase="keep"`, and `Wallet.owner` is PROTECT so a
        deletion cannot destroy a balance in the first place.

        Raises `ErasureRefused` (code `trainer_has_active_clients`) for a trainer who
        still coaches someone: the owner's rule, and nothing is changed.
        """
        from training_platform.privacy import erase_user_data

        report = erase_user_data(self)
        self.refresh_from_db()
        logger.info("Retired user %s (%s): %s", self.pk, reason or "no reason given", report)
        return self

    def save(self, *args, **kwargs):
        """Override save to ensure data consistency."""
        # Ensure admins have staff permissions
        if self.is_admin:
            self.is_staff = True
            
        # Ensure trainers can't be assigned to other trainers
        if self.is_trainer and self.assigned_trainer:
            self.assigned_trainer = None

        # `age` follows the date of birth whenever there is one; see `date_of_birth`.
        if self.date_of_birth:
            derived = age_on(self.date_of_birth)
            if self.age != derived:
                self.age = derived
                update_fields = kwargs.get('update_fields')
                if update_fields is not None and 'age' not in update_fields:
                    kwargs['update_fields'] = [*update_fields, 'age']

        # Replaced-file cleanup lives in training_platform.signals, which defers the
        # delete until the transaction commits and skips files another row still
        # references. Doing it here destroyed the old picture even when the save was
        # later rolled back, leaving the row pointing at a missing file.
        super().save(*args, **kwargs)

    # NOTE: Diet plan generation is handled by the diet app's GPT-based system.
    # This model does not generate diet plans directly. Use the diet app API or Celery task for plan creation.

class TrainerClientRelation(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    trainer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='trainer_relations',
        limit_choices_to={'user_type': 'trainer'}
    )
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='client_relations',
        limit_choices_to={'user_type': 'client'}
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    #: Who asked. Without it, "approve this pending request" could be aimed at a row
    #: the approver had created themselves: a trainer called assign-client on any
    #: unassigned client, then approved their own row, and owned that person's body
    #: metrics, training history and diet plan without the client ever being asked.
    #: A request is approved by the OTHER party, and this is what says who that is.
    #: 'unknown' is for rows written before this column existed, and it is the
    #: DEFAULT on purpose: a code path that forgets to say who asked produces a row
    #: neither side can approve, rather than one the trainer can approve for
    #: themselves. Failing safe is the whole point of the column.
    INITIATOR_CHOICES = [('client', 'Client'), ('trainer', 'Trainer'),
                         ('unknown', 'Unknown (predates this field)')]
    initiated_by = models.CharField(
        max_length=8, choices=INITIATOR_CHOICES, default='unknown', db_index=True,
        help_text="Which side asked. The other side approves.")
    #: The price both sides agreed to, snapshotted when the request was made. The
    #: charge used to be re-read from `trainer_hourly_rate` at approval time, and that
    #: field is trainer-writable: a trainer could advertise 10, wait for the request,
    #: raise the rate, then approve and sweep the client's wallet. The client agreed
    #: to a number, so that number is what they pay.
    agreed_rate = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    #: The UNIT of `agreed_rate`: the trainer's currency when the price was quoted,
    #: written beside it every time it is written. A bare number was read in whatever
    #: currency the trainer's wallet had at hold time, so a trainer who invited at
    #: 500000 (pounds) and then switched to dollars charged the client 500000 dollars
    #: (SIDE_BUGS S-083). Blank only on a row whose `agreed_rate` is empty too.
    agreed_currency = models.CharField(max_length=3, blank=True, default='')
    #: The reference of the escrow Transaction while money is held for this pair, and
    #: empty when nothing is held. The hold had no identity at all: it was found by
    #: searching transaction metadata, released nowhere, and settled by the routine
    #: app from the shared escrow pool at whatever the trainer's rate happened to be —
    #: so assigning fifty routines paid out fifty times, out of other clients' money.
    #: One hold, named here, is what makes taking it, giving it back and paying it out
    #: the same single event.
    hold_reference = models.CharField(max_length=36, blank=True, default='', db_index=True)
    #: When the request now pending (or last answered) was sent. Not `created_at`: a
    #: declined request sent again reuses this row, so `created_at` is when the pair
    #: first met, not when this request was made.
    requested_at = models.DateTimeField(null=True, blank=True)
    #: The trainer asked to stop at the end of the paid month. A trainer cannot end a
    #: month the client is in (owner, 2026-09-19); the renewal then ends the coaching
    #: instead of charging the next month (users/coaching.py).
    ends_at_period_end = models.BooleanField(default=False)
    #: The CLIENT asked not to renew after the paid month (Road to 10 S2.7, the owner's
    #: D12): «لا تجدّد بعد هذا الشهر». The renewal then ends the coaching instead of
    #: charging the next month — nothing is charged or held, and the month already paid
    #: stays the trainer's. The client sets and unsets it (`POST /api/v1/coaching/
    #: renewal/`) until the renewal run reaches the month's end; the trainer only reads it.
    #: Apart from `ends_at_period_end`, which is the trainer's and cannot be withdrawn.
    client_stops_renewal = models.BooleanField(default=False)
    #: The note sent with the CURRENT request, written by the side `initiated_by` names:
    #: what the client wrote with a request ("introduce yourself", RequestSheet), or what
    #: the trainer wrote with an invitation (TrainerInvite, BACKEND_BUGS B-093). The name
    #: predates invitations carrying one; read the author from `initiated_by`, never
    #: from the column's name. Encrypted: people describe injuries here. Shown to its
    #: author, and to the other side of that request unless either has blocked the
    #: other; replaced by each new request or invitation.
    client_note = EncryptedTextField(blank=True, default='')
    #: When the other side approved or declined it ("training together since"). Each
    #: answer is also written to `TrainerRequestOutcome`, which outlives this row and
    #: is what response times are measured from.
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']
        unique_together = ('trainer', 'client')
        verbose_name = 'Trainer-Client Relation'
        verbose_name_plural = 'Trainer-Client Relations'
        constraints = [
            # A client can have at most one approved trainer
            models.UniqueConstraint(
                fields=['client'],
                condition=Q(status='approved'),
                name='unique_approved_trainer_per_client'
            ),
            # A client can have at most one pending request OF THEIR OWN. Scoped to
            # client-initiated rows: a trainer's invitation is a different thing, and
            # counting it here meant any trainer could freeze any client out of
            # choosing anybody — the client's own request hit this index and 500'd —
            # while a client could also receive only one invitation in total.
            models.UniqueConstraint(
                fields=['client'],
                condition=Q(status='pending', initiated_by='client'),
                name='unique_pending_trainer_request_per_client'
            ),
        ]

    def __str__(self):
        return f"{self.trainer.username} ↔ {self.client.username} ({self.status})"


class TrainerRequestOutcome(models.Model):
    """One answered request: who asked whom, when, and how long the answer took.

    Append-only, written in the same transaction as the approval or decline. The
    relation row itself is deleted when a client leaves, cancels or is erased, so a
    statistic read from relations would quietly lose the requests that went well. This
    is the record a trainer's typical response time is computed from
    (`users/trainer_proof.py`): platform timestamps only, never a number anyone types.
    """
    OUTCOME_CHOICES = [('approved', 'Approved'), ('rejected', 'Declined')]

    trainer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='request_outcomes_as_trainer')
    client = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                               related_name='request_outcomes_as_client')
    #: Which side asked. Response time is the OTHER side's; the trainer's statistic
    #: reads only rows the client initiated.
    initiated_by = models.CharField(max_length=8, choices=TrainerClientRelation.INITIATOR_CHOICES)
    outcome = models.CharField(max_length=8, choices=OUTCOME_CHOICES)
    requested_at = models.DateTimeField()
    responded_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ['-responded_at', '-id']
        indexes = [models.Index(fields=['trainer', 'initiated_by', '-responded_at'],
                                name='outcome_trainer_recent_idx')]
        constraints = [
            models.CheckConstraint(condition=Q(responded_at__gte=models.F('requested_at')),
                                   name='outcome_answered_after_asked'),
        ]

    def __str__(self):
        return f"{self.trainer_id} ← {self.client_id}: {self.outcome}"

    @property
    def response_seconds(self):
        return (self.responded_at - self.requested_at).total_seconds()


class CoachingEnding(models.Model):
    """Why a coaching ended, for the operators (BACKEND_BUGS B-159, B-160).

    The leave sheets ask for a reason — the client's «(اختياري، يساعدنا)», the trainer's
    «يراه فريق المنصة فقط» — and the relation row is deleted when the coaching ends, so
    this is the only place it can be kept. One row per ending, written in the same
    transaction that ends it (`record`): the client leaving
    (`client/unassign-trainer/`), the trainer ending it now, or the trainer SCHEDULING
    the end at the month's close (`scheduled`; the relation ends later, in the renewal
    run, and writes nothing more). A client's «do not renew» (S2.7) is written by the
    renewal run when it ends the coaching (`ended_by` client, `scheduled`, no reason).

    The owner (2026-09-28): the reason codes are the ones drawn on each sheet; the row
    is seen by OPERATORS only — the console, read-only — never by the trainer, and a
    trainer's reason is never shown to the client; nothing counts them yet. The reason
    is optional: an unknown one is stored as NULL (and logged), never refused, so it
    never stands between anyone and leaving.

    Append-only: nothing edits a row, except an erasure, which removes the link to BOTH
    people and keeps who ended it, the reason and the date (`training_platform/privacy/
    sources.py`).
    """
    ENDED_BY_CHOICES = [('client', 'Client'), ('trainer', 'Trainer')]
    #: The client's leave sheet.
    CLIENT_REASONS = ('price', 'slow_replies', 'other_specialty', 'other')
    #: The trainer's remove sheet.
    TRAINER_REASONS = ('not_committed', 'no_reply', 'roster_full', 'other')
    REASON_CHOICES = [
        ('price', 'Price (client)'),
        ('slow_replies', 'Slow replies (client)'),
        ('other_specialty', 'Wants another specialty (client)'),
        ('not_committed', 'Not committed (trainer)'),
        ('no_reply', 'Does not reply (trainer)'),
        ('roster_full', 'Roster full (trainer)'),
        ('other', 'Other'),
    ]

    trainer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                null=True, blank=True,
                                related_name='coaching_endings_as_trainer')
    client = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                               null=True, blank=True,
                               related_name='coaching_endings_as_client')
    ended_by = models.CharField(max_length=8, choices=ENDED_BY_CHOICES)
    reason = models.CharField(max_length=16, choices=REASON_CHOICES, null=True, blank=True)
    #: The end came at the close of the running month, not now: the trainer scheduled it
    #: (written when scheduled, the relation still approved), or the client asked not to
    #: renew (S2.7; written by the renewal run that ends it, with no reason, because the
    #: client could withdraw the choice until then).
    scheduled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f"{self.trainer_id} / {self.client_id}: ended by {self.ended_by} ({self.reason})"

    @classmethod
    def record(cls, *, trainer, client, ended_by, reason, scheduled=False):
        """Write one ending. `reason` is the body's value as sent: kept when it is one of
        `ended_by`'s own codes, NULL otherwise (an unknown value is logged)."""
        allowed = cls.CLIENT_REASONS if ended_by == 'client' else cls.TRAINER_REASONS
        code = reason if isinstance(reason, str) and reason in allowed else None
        if code is None and reason not in (None, ''):
            logger.info("Coaching ending: unknown %s reason %r stored as none",
                        ended_by, str(reason)[:32])
        return cls.objects.create(trainer=trainer, client=client, ended_by=ended_by,
                                  reason=code, scheduled=scheduled)


class CoachingPeriod(models.Model):
    """One month of one-to-one coaching, and what happened to the money for it.

    Coaching is sold by the month (owner, 2026-09-19). Month 1 is held in escrow when the
    trainer accepts (`take_hold`) and paid out when the first program arrives
    (`settle_hold`); that moment starts the month. Each later month is charged from the
    client's wallet when the one before it ends and paid out at once
    (`users/coaching.py`). Every payout splits the price: `commission` to the platform at
    the `commission_rate` copied here when the month was charged, the rest to the
    trainer.

    A money record: kept on erasure, and it outlives the relation it belongs to (a
    client who leaves deletes the relation row, never this).
    """
    STATUS_CHOICES = [
        ('held', 'Held in escrow'),       # month 1, before the first program
        ('settled', 'Paid out'),          # trainer and platform paid; the month runs
        ('refunded', 'Returned to the client'),
    ]

    trainer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                related_name='coaching_periods_as_trainer')
    client = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                               related_name='coaching_periods_as_client')
    relation = models.ForeignKey(TrainerClientRelation, on_delete=models.SET_NULL,
                                 null=True, blank=True, related_name='periods')
    #: 1 for the first month of this relation, then 2, 3, ...
    number = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    #: The unit of `price`, `commission` and `trainer_share`: the CLIENT's currency,
    #: which is what was charged (the owner, 2026-09-21 — a person pays in the one
    #: currency they trade in, whatever the trainer prices in). A price without a
    #: currency is not a price, and the payout needs it twice: to find the escrow
    #: wallet holding this month and the platform wallet its commission belongs in,
    #: both of which are per currency (`wallet.utils`). Copied off the hold's own
    #: ledger row when the month is opened, never read back from a setting.
    currency = models.CharField(max_length=8, default='SYP')
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4)
    commission = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    trainer_share = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    #: THE PRICE OF RECORD this month was charged for, and the rate that turned it into
    #: `price` (SIDE_BUGS S-151). The trainer prices in their currency and the client
    #: pays in theirs, so a month charged across the two is a conversion, and every
    #: conversion is snapshotted: without these a USD-priced month charged in SYP could
    #: not be explained on a receipt or by support, and every renewal re-charged the
    #: number with no record of how it was reached. Same shape as `subscription.Payment`
    #: (`price_amount`, `price_currency`, `exchange_rate`, `exchange_rate_value`).
    #: A renewal copies month 1's: it is charged at the same client-currency price.
    #: Null on months written before this existed; `exchange_rate` is null too when
    #: nothing was converted.
    price_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    price_currency = models.CharField(max_length=3, blank=True, default='')
    exchange_rate = models.ForeignKey('wallet.ExchangeRate', null=True, blank=True,
                                      on_delete=models.PROTECT,
                                      related_name='coaching_periods')
    exchange_rate_value = models.DecimalField(max_digits=18, decimal_places=6,
                                              null=True, blank=True)
    #: The trainer's share of a paid month is still in escrow, owed: it had to cross a
    #: currency and the operator's rate was missing or stale when the month was paid
    #: out (SIDE_BUGS S-082). The client's charge and the platform's commission are
    #: not held up by it — the month runs — and the hourly run pays the share once the
    #: rate is fresh (`users.coaching.settle_owed_payouts`). Null when nothing is owed.
    payout_owed_since = models.DateTimeField(null=True, blank=True, db_index=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='held',
                              db_index=True)
    #: The client -> escrow ledger row for this month.
    hold_reference = models.CharField(max_length=36, unique=True)
    payout_reference = models.CharField(max_length=36, blank=True, default='')
    commission_reference = models.CharField(max_length=36, blank=True, default='')
    #: Set when the month is paid out. Month 1 starts then; a renewal starts where the
    #: month before it ended, so the cycle never drifts.
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True, db_index=True)
    #: When the client was told this month renews soon (COACHING_RENEWAL_REMINDER_DAYS
    #: before `ends_at`; the owner, 2026-09-19). Set once, so the notice is sent once.
    renewal_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    #: When the renewal after this month was tried and the wallet could not cover it:
    #: the coaching is paused (frozen) from `ends_at` until a month is paid. The client
    #: keeps seeing the trainer's routines and cannot start them (the owner, 2026-09-19).
    #: A renewal paid later starts a new month at the moment of payment.
    frozen_at = models.DateTimeField(null=True, blank=True)
    #: How many "still paused" reminders have gone out after the freeze notice, one per
    #: step of COACHING_FROZEN_REMINDER_DAYS.
    frozen_notices = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            # One month per (pair, start): two workers renewing the same month collide
            # here instead of charging twice.
            models.UniqueConstraint(fields=['trainer', 'client', 'starts_at'],
                                    condition=Q(starts_at__isnull=False),
                                    name='one_coaching_period_per_start'),
            models.UniqueConstraint(fields=['relation', 'number'],
                                    condition=Q(relation__isnull=False),
                                    name='one_coaching_period_number_per_relation'),
            models.CheckConstraint(condition=Q(price__gt=0), name='coaching_period_price_positive'),
            models.CheckConstraint(
                condition=Q(commission_rate__gte=0) & Q(commission_rate__lte=1),
                name='coaching_commission_rate_is_a_fraction'),
        ]
        indexes = [models.Index(fields=['relation', '-number'], name='period_relation_latest_idx')]

    def __str__(self):
        return f"{self.trainer_id}/{self.client_id} month {self.number} ({self.status})"


class DeviceToken(models.Model):
    PLATFORM_CHOICES = [
        ('android', 'Android'),
        ('ios', 'iOS'),
        ('web', 'Web'),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='device_tokens')
    token = models.CharField(max_length=255, unique=True, db_index=True)
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES, default='android')
    #: The BUILD that registered this token: the `X-App-Channel` its request declared
    #: (`policy.channels.Channel`: apk, play, ios, web), or null when it declared none
    #: (every token registered before 2026-11-24). A push gives this device the text its
    #: build is shown (`notifications/build_gate.py`, Road to 10 R1); null is read as the
    #: strictest build, or as ios for `platform` ios.
    app_channel = models.CharField(max_length=8, blank=True, null=True)
    app_version = models.CharField(max_length=50, blank=True, null=True)
    device_id = models.CharField(max_length=255, blank=True, null=True)
    is_active = models.BooleanField(default=True, db_index=True, help_text="Soft delete for invalid tokens")
    last_used_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['user', 'is_active']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.token[:10]}... ({'Active' if self.is_active else 'Inactive'})"

    def mark_inactive(self):
        """Soft delete the token."""
        self.is_active = False
        self.save(update_fields=['is_active'])

class OTPVerification(models.Model):
    """
    Model to store OTP codes for email verification and password reset.
    OTP expires after 10 minutes and can only be used once.
    """
    PURPOSE_CHOICES = [
        ('registration', 'Registration'),
        ('password_reset', 'Password Reset'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='otp_verifications'
    )
    # Holds the keyed HMAC-SHA256 of the code (64 hex chars), never the code itself.
    # This was max_length=6 — the width of the plaintext code — long after the value
    # became a hash, so Postgres rejected every insert with
    # "value too long for type character varying(6)" and registration could not
    # complete at all. SQLite ignores CharField width, which is why it survived.
    otp_code = models.CharField(max_length=64, help_text="Keyed HMAC-SHA256 of the 6-digit code")
    email = models.EmailField(help_text="Email address for lookup")
    purpose = models.CharField(
        max_length=20,
        choices=PURPOSE_CHOICES,
        default='registration',
        help_text="Purpose of this OTP (registration or password_reset)"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(help_text="OTP expiration time (10 minutes from creation)")
    is_verified = models.BooleanField(default=False, help_text="Whether this OTP has been used")
    verified_at = models.DateTimeField(null=True, blank=True, help_text="When OTP was verified")

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']
        verbose_name = "OTP Verification"
        verbose_name_plural = "OTP Verifications"
        indexes = [
            models.Index(fields=['email', 'otp_code', 'is_verified']),
            models.Index(fields=['expires_at']),
            models.Index(fields=['purpose', 'is_verified']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'otp_code'],
                condition=models.Q(is_verified=False),
                name='unique_unverified_otp_per_user'
            ),
        ]

    def __str__(self):
        return f"OTP ({self.purpose}) for {self.email} - {self.otp_code} ({'verified' if self.is_verified else 'pending'})"

    def is_valid(self):
        """Check if OTP is still valid (not expired and not used)."""
        from django.utils import timezone
        return not self.is_verified and timezone.now() < self.expires_at


class PasswordResetToken(models.Model):
    """
    Single-use token issued after successful OTP verification for password reset.
    The token is consumed when the user submits a new password.
    Expires after 15 minutes.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='password_reset_tokens'
    )
    # The raw token is NEVER stored. It used to be a plain UUIDField written verbatim,
    # so anyone who could read this table — a backup, a replica, a support export, a
    # SQL-injection foothold — could complete a password reset for any account with a
    # pending token. `OTPVerification` already hashed its code; this did not.
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(help_text="Token expiration time (15 minutes from creation)")
    is_used = models.BooleanField(default=False, help_text="Whether this token has been consumed")
    used_at = models.DateTimeField(null=True, blank=True, help_text="When the token was consumed")

    RAW_TOKEN_BYTES = 32

    @staticmethod
    def hash_token(raw_token: str) -> str:
        """Keyed hash of a reset token.

        HMAC rather than a bare digest: the key lives in SECRET_KEY, so a stolen
        database alone cannot be used to derive a usable token.
        """
        import hashlib
        import hmac

        from django.conf import settings

        return hmac.new(
            settings.SECRET_KEY.encode("utf-8"),
            (raw_token or "").strip().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @classmethod
    def issue(cls, user, minutes: int = 15):
        """Create a token and return (instance, raw_token). The raw value is returned
        to the caller once and never persisted."""
        import secrets

        from django.utils import timezone

        raw = secrets.token_urlsafe(cls.RAW_TOKEN_BYTES)
        instance = cls.objects.create(
            user=user,
            token_hash=cls.hash_token(raw),
            expires_at=timezone.now() + timedelta(minutes=minutes),
        )
        return instance, raw

    @classmethod
    def verify(cls, raw_token: str):
        """Return the matching token row, or None. Lookup is by hash."""
        if not raw_token:
            return None
        try:
            return cls.objects.select_related("user").get(token_hash=cls.hash_token(raw_token))
        except cls.DoesNotExist:
            return None

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']
        verbose_name = "Password Reset Token"
        verbose_name_plural = "Password Reset Tokens"
        indexes = [
            models.Index(fields=['token_hash', 'is_used']),
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f"Reset token for {self.user.email} ({'used' if self.is_used else 'active'})"

    def is_valid(self):
        """Check if token is still valid (not expired and not used)."""
        return not self.is_used and timezone.now() < self.expires_at

