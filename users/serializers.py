# users/serializers.py

from dj_rest_auth.registration.serializers import RegisterSerializer
from dj_rest_auth.serializers import LoginSerializer
from .models import (CustomUser, DeviceToken, INJURY_AREAS, TRAINER_FOCUS, TRAINING_PLACES,
                     age_on, minimum_age)
from rest_framework import serializers
import logging
import re  # Add this import
from django.db import IntegrityError
from django.conf import settings
from django.core.files.images import get_image_dimensions
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------------
# Date of birth and the onboarding answers (design v2)
# ---------------------------------------------------------------------------------
#: A date further back than this is a typo or a test, not a person's birthday.
OLDEST_PLAUSIBLE_AGE = 100


def check_date_of_birth(value):
    """Refuse a date of birth the platform must not accept. Codes (field-level):
    `date_of_birth_in_future`, `date_of_birth_implausible`, `under_minimum_age`."""
    today = timezone.localdate()
    if value > today:
        raise serializers.ValidationError(_("The date of birth cannot be in the future."),
                                          code="date_of_birth_in_future")
    age = age_on(value, today)
    if age > OLDEST_PLAUSIBLE_AGE:
        raise serializers.ValidationError(_("Enter your real date of birth."),
                                          code="date_of_birth_implausible")
    if age < minimum_age():
        raise serializers.ValidationError(
            _("You must be at least %(age)s years old to use Yalla Gym.") % {"age": minimum_age()},
            code="under_minimum_age")
    return value


def _code_list(choices, *, allow_none=False):
    codes = [c for c, _label in choices] + (["none"] if allow_none else [])
    return serializers.ListField(child=serializers.ChoiceField(choices=codes),
                                 required=False, allow_empty=True)


def _unique_codes(value, *, none_alone=False):
    """No code twice; `none` (when allowed) means the empty list and stands alone."""
    if len(set(value)) != len(value):
        raise serializers.ValidationError(_("A code is listed twice."), code="duplicate")
    if none_alone and "none" in value:
        if len(value) > 1:
            raise serializers.ValidationError(
                _("'none' cannot be combined with an injury area."), code="none_with_areas")
        return []
    return value


#: The free-text injury note's limit (BACKEND_BUGS B-028), the app's own cap.
SPECIFIC_INJURY_MAX_LENGTH = 500

CLIENT_ONBOARDING_FIELDS = ('training_goal', 'training_place', 'training_days_per_week',
                            'injury_areas')
TRAINER_MATCHING_FIELDS = ('trainer_goal_focus', 'trainer_injury_focus',
                           'trainer_training_places')


class OnboardingFieldsMixin(serializers.Serializer):
    """The v2 onboarding answers and the date of birth, shared by the profile serializers.

    Writing: `date_of_birth` (ISO date, see `check_date_of_birth`), `training_goal`,
    `training_place`, `training_days_per_week` (1-7), `injury_areas` (a list of codes;
    `["none"]` or `[]` means none). Once a date of birth is known, a written `age` is
    ignored: `age` is derived from the date. A legacy `age` below the minimum is refused
    with `under_minimum_age`.

    Reading adds `onboarding_missing`: the fields still unanswered, in screen order
    (`activity_level` is listed until it is written, although it has a default).
    `onboarding_completed` keeps its original meaning. `specific_injury` is at most 500
    characters (`too_long`).
    """
    injury_areas = _code_list(INJURY_AREAS, allow_none=True)
    onboarding_missing = serializers.SerializerMethodField()
    # Declared without the model's MinValueValidator(13): that floor answered `min_value`
    # for every age under 13, before `validate_age` could say `under_minimum_age`.
    age = serializers.IntegerField(required=False, allow_null=True, max_value=100)

    def validate_date_of_birth(self, value):
        if value is None:
            # Clearing it would reopen the legacy `age` field to a younger number.
            raise serializers.ValidationError(_("The date of birth cannot be removed."),
                                              code="date_of_birth_required")
        return check_date_of_birth(value)

    def validate_injury_areas(self, value):
        return _unique_codes(value, none_alone=True)

    def validate_specific_injury(self, value):
        # Bounded (BACKEND_BUGS B-028): unbounded, one save could store up to the 15 MB
        # body cap of encrypted text, and the same text goes to the trainer's view.
        if value and len(value) > SPECIFIC_INJURY_MAX_LENGTH:
            raise serializers.ValidationError(
                _("Keep this to %(n)s characters.") % {"n": SPECIFIC_INJURY_MAX_LENGTH},
                code="too_long")
        return value

    def validate_age(self, value):
        if value is not None and value < minimum_age():
            raise serializers.ValidationError(
                _("You must be at least %(age)s years old to use Yalla Gym.") % {"age": minimum_age()},
                code="under_minimum_age")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        born = attrs.get('date_of_birth') or getattr(self.instance, 'date_of_birth', None)
        if born:
            attrs.pop('age', None)
        if 'activity_level' in attrs:
            # The level has a default, so the value alone cannot say it was answered.
            attrs['activity_level_answered_at'] = timezone.now()
        return attrs

    def get_onboarding_missing(self, obj):
        missing = [f for f in ('first_name', 'last_name', 'date_of_birth') if not getattr(obj, f)]
        if obj.is_client:
            missing += [f for f in ('gender', 'height', 'weight') if not getattr(obj, f)]
            # `activity_level` defaults to Sedentary, so "never asked" and "beginner"
            # looked the same (BACKEND_BUGS B-027). Unanswered until written through the
            # API — or, for a row from before, until it differs from the default.
            if obj.activity_level_unanswered:
                missing.append('activity_level')
            missing += [f for f in ('training_goal', 'training_place', 'training_days_per_week')
                        if not getattr(obj, f)]
        return missing


class TrainerMatchingFieldsMixin(serializers.Serializer):
    """What a trainer coaches, as codes the matcher compares with a client's answers:
    `trainer_goal_focus` (goals + `nutrition`), `trainer_injury_focus`,
    `trainer_training_places`. Lists of codes, each at most once."""
    trainer_goal_focus = _code_list(TRAINER_FOCUS)
    trainer_injury_focus = _code_list(INJURY_AREAS)
    trainer_training_places = _code_list(TRAINING_PLACES)

    def validate_trainer_goal_focus(self, value):
        return _unique_codes(value)

    def validate_trainer_injury_focus(self, value):
        return _unique_codes(value)

    def validate_trainer_training_places(self, value):
        return _unique_codes(value)

class CustomRegisterSerializer(RegisterSerializer):
    """POST /api/auth/register/.

    `date_of_birth` (ISO date) is required: the server refuses anyone younger than
    `MINIMUM_AGE_YEARS` (14). Field codes on `field_errors.date_of_birth`:
    `required`, `under_minimum_age`, `date_of_birth_in_future`,
    `date_of_birth_implausible`. Required by the owner's decision (2026-09-19); no build
    that omits it was ever released. An address or username already registered answers
    `email_taken` or `username_taken` on its field (a malformed address stays
    `invalid`; phone numbers are not unique); passwords answer the validators' codes
    (`password_too_short`, `password_too_common`, `password_entirely_numeric`).
    """
    phone_number = serializers.CharField(max_length=15,  required=True)
    user_type = serializers.ChoiceField(choices=[('client', 'Client'), ('trainer', 'Trainer'), ('agent', 'Agent'), ('admin', 'Administrator')], required=False, default='client')
    date_of_birth = serializers.DateField(required=True)

    def validate_date_of_birth(self, value):
        return check_date_of_birth(value)

    def validate_phone_number(self, value):
        # Stored with Western digits: `\d` also matched Arabic-Indic digits, so
        # "+٩٦٣..." was kept as typed and matched nothing later (B-015).
        from .utils import western_digits
        value = western_digits(value)
        phone_regex = re.compile(r'^\+?1?[0-9]{9,15}$')
        if not phone_regex.match(value):
            raise serializers.ValidationError(
                _("Phone number must be entered in the format: '+999999999'. Up to 15 digits allowed.")
            )
        return value
    
    def validate_user_type(self, value):
        request = self.context.get('request')
        # Privileged roles cannot be self-provisioned. 'agent' has wallet +
        # API-key access; 'admin' is a superuser. Both require an authenticated
        # superuser to create.
        if value in ('admin', 'agent'):
            if not request or not request.user.is_authenticated or not request.user.is_superuser:
                raise serializers.ValidationError(_("This user type cannot be self-registered."))
        # Only client/trainer are valid for self-service registration
        if value not in ['client', 'trainer', 'agent', 'admin']:
            raise serializers.ValidationError(_("Invalid user type."))
        return value

    def validate_email(self, value):
        # Its own code (BACKEND_BUGS B-020): `invalid` is also what a malformed address
        # gets, and the app branches on the code. Without regard to case: addresses are
        # stored lower-cased, so `Samer@x.com` beside `samer@x.com` passed here and
        # failed at the database.
        # Lower-cased or as typed, never `iexact` (see `users.utils.find_by_email`).
        typed = (value or '').strip()
        if CustomUser.objects.filter(email__in={typed.lower(), typed}).exists():
            raise serializers.ValidationError(_("A user with this email already exists."),
                                              code='email_taken')
        return value

    class Meta:
        model = CustomUser
        fields = ['username', 'email', 'phone_number', 'password1', 'password2', 'user_type',
                  'date_of_birth']

    def save(self, request):
        logger.info("CustomRegisterSerializer.save() called")
        try:
            user = super().save(request)
            user.phone_number = self.validated_data.get('phone_number')
            user.user_type = self.validated_data.get('user_type', 'client')
            user.date_of_birth = self.validated_data.get('date_of_birth')
            if user.user_type == 'trainer':
                # Closed until the trainer opens: `trainer_is_available` defaults to True,
                # so an empty profile was listed, and matched, the moment the address was
                # verified (BACKEND_BUGS B-010). The trainer switches it on from their
                # profile once there is something to show.
                user.trainer_is_available = False
            # Unverified, and NOT disabled. This wrote `is_active = False` to mean "not
            # verified yet", which is also what a suspension writes, so resend-otp could
            # not tell a pending signup from a suspended account and refused both —
            # measured: the first code expires, resend answers 200 and sends nothing,
            # and the account can never be verified, logged into or registered again.
            # Credentials are withheld on `email_verified` instead (login, token).
            user.is_active = True
            user.email_verified = False
            user.save()
            return user
        except IntegrityError as e:
            # Match the FIELD name, not a backend's phrasing. These tests were written
            # against SQLite ("UNIQUE constraint failed: users_customuser.email") and
            # never matched on Postgres, which says
            #   duplicate key value violates unique constraint "users_customuser_email_6445acef_uniq"
            # so in production a duplicate that slipped past field validation — two
            # simultaneous signups with the same email — surfaced as a 500 instead of
            # the 400 this handler exists to produce. The field name appears in both
            # backends' messages.
            message = str(e).lower()
            if "phone_number" in message:
                raise serializers.ValidationError({"phone_number": [serializers.ErrorDetail(
                    _("A user with this phone number already exists."), code='phone_taken')]})
            if "email" in message:
                raise serializers.ValidationError({"email": [serializers.ErrorDetail(
                    _("A user with this email already exists."), code='email_taken')]})
            if "username" in message:
                raise serializers.ValidationError({"username": [serializers.ErrorDetail(
                    _("A user with that username already exists."), code='username_taken')]})
            # Was `raiser`: any other integrity error became a NameError and a 500 that
            # hid the constraint that actually failed.
            raise
    
    
    

class CustomLoginSerializer(LoginSerializer):
    username = None  # Remove the username field

    email = serializers.EmailField(required=True)  # Ensure email is required

    def validate(self, attrs):
        logger.info("CustomLoginSerializer.validate() called")
        email = attrs.get('email')
        password = attrs.get('password')

        if email and password:
            from .utils import check_login_lockout, record_login_failure, clear_login_attempts

            # Per-account brute-force lockout (5 failures → 15-min cooldown; users/utils.py)
            is_locked, _remaining = check_login_lockout(email)
            if is_locked:
                from .exceptions import TooManyAttempts
                from .utils import lockout_seconds_left
                raise TooManyAttempts(lockout_seconds_left(email))

            # Authenticate using email and password, with the address as stored
            # (BACKEND_BUGS B-033): a different case was refused and counted a failure.
            from .utils import find_by_email
            found = find_by_email(email)
            self.user = self.authenticate(email=found.email if found else email,
                                          password=password)
            if not self.user:
                # The right password on a suspended account: say so (owner, B-004).
                from .exceptions import AccountDisabled, disabled_account_with_password
                if disabled_account_with_password(email, password):
                    raise AccountDisabled()
                record_login_failure(email)
                raise serializers.ValidationError(
                    {"non_field_errors": [_("Unable to log in with provided credentials.")]}
                )

            # Verified, not merely enabled. This tested `is_active`, which the backend
            # had already refused above, so the branch was dead: a pending signup was
            # told "Unable to log in with provided credentials" and never routed to the
            # code screen. A disabled account still stops at `authenticate`.
            if not self.user.email_verified:
                raise serializers.ValidationError(
                    {
                        "non_field_errors": [_("Please verify your email address before logging in. Check your inbox for the OTP code.")],
                        "requires_verification": True,
                        "email": self.user.email
                    }
                )

            # Successful auth — reset the failure counter
            clear_login_attempts(email)
        else:
            raise serializers.ValidationError(
                {"non_field_errors": [_("Must include 'email' and 'password'.")]}
            )

        attrs['user'] = self.user
        return attrs
    
class UserDetailsSerializer(OnboardingFieldsMixin, TrainerMatchingFieldsMixin,
                            serializers.ModelSerializer):
    profile_picture = serializers.SerializerMethodField()
    onboarding_completed = serializers.BooleanField(source='is_onboarding_completed', read_only=True)
    
    class Meta:
        model = CustomUser
        fields = [
            'height', 'weight', 'age', 'gender', 'specific_injury',
            'activity_level', 'user_type', 'first_name', 'last_name', 'profile_picture',
            'trainer_bio', 'trainer_specializations', 'trainer_certifications',
            'trainer_experience_years', 'trainer_hourly_rate', 'trainer_is_verified',
            'trainer_is_available', 'assigned_trainer', 'client_goals', 'client_preferences',
            'is_active', 'onboarding_completed',
            # Scheduling. `preferred_timezone` existed but was read by nothing, so
            # "localized dates and times" never happened; it now decides when the
            # workout reminder fires in the user's own day.
            'preferred_timezone', 'workout_reminder_hour',
            # Design v2: see OnboardingFieldsMixin and TrainerMatchingFieldsMixin.
            'date_of_birth', *CLIENT_ONBOARDING_FIELDS, 'onboarding_missing',
            *TRAINER_MATCHING_FIELDS,
            # The language of this person's e-mails and pushes (`ar` | `en`). Nothing could
            # set it, so every one of them went out in English (BACKEND_BUGS B-005).
            'preferred_language',
        ]
        # assigned_trainer is read-only here: trainer assignment must go through
        # the request/approval + wallet-escrow flow, never self-service update.
        read_only_fields = ['user_type', 'trainer_is_verified', 'is_active', 'onboarding_completed', 'assigned_trainer']

    def get_profile_picture(self, obj):
        request = self.context.get('request')
        if obj.profile_picture:
            url = obj.profile_picture.url
            if request is not None:
                return request.build_absolute_uri(url)
            return url
        return None

    # `validate_profile_picture` stood here and never ran: `profile_picture` is a
    # read-only method field, so a file posted to user/update/ was dropped (BACKEND_BUGS
    # B-026). Pictures go through `POST /api/auth/user/profile-picture/`, which checks
    # the content; this serializer only reads the URL.

    def validate(self, attrs):
        attrs = super().validate(attrs)
        user = self.instance
        if user and user.user_type == 'trainer':
            # Trainers can't have assigned_trainer
            if 'assigned_trainer' in attrs:
                attrs.pop('assigned_trainer')
            # ...nor a client's onboarding answers.
            for field in CLIENT_ONBOARDING_FIELDS:
                attrs.pop(field, None)
        elif user and user.user_type == 'client':
            # Clients can't have trainer-specific fields
            trainer_fields = ['trainer_bio', 'trainer_specializations', 'trainer_certifications',
                            'trainer_experience_years', 'trainer_hourly_rate', 'trainer_is_available',
                            *TRAINER_MATCHING_FIELDS]
            for field in trainer_fields:
                if field in attrs:
                    attrs.pop(field)
        return attrs

class TrainerProfileSerializer(TrainerMatchingFieldsMixin, serializers.ModelSerializer):
    """Serializer for trainer-specific profile data.

    Also takes the matching codes (`trainer_goal_focus`, `trainer_injury_focus`,
    `trainer_training_places`), which decide which clients the trainer is matched with
    and why (`GET /api/v1/trainers/matches`).
    """
    client_count = serializers.SerializerMethodField()
    profile_picture = serializers.SerializerMethodField()
    
    onboarding_completed = serializers.BooleanField(source='is_onboarding_completed', read_only=True)

    class Meta:
        model = CustomUser
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 'profile_picture',
            'trainer_bio', 'trainer_specializations', 'trainer_certifications',
            'trainer_experience_years', 'trainer_hourly_rate', 'trainer_is_verified',
            'trainer_is_available', 'client_count', 'is_active', 'onboarding_completed',
            *TRAINER_MATCHING_FIELDS,
        ]
        read_only_fields = ['id', 'username', 'email', 'trainer_is_verified', 'is_active', 'onboarding_completed']

    def get_client_count(self, obj):
        return obj.get_client_count()

    def get_profile_picture(self, obj):
        request = self.context.get('request')
        if obj.profile_picture:
            url = obj.profile_picture.url
            if request is not None:
                return request.build_absolute_uri(url)
            return url
        return None

    # No `validate_profile_picture`: `profile_picture` is a read-only method field, so
    # one here never ran (SIDE_BUGS S-150, as B-026 for UserDetailsSerializer).
    # Pictures go through `POST /api/auth/user/profile-picture/`, which checks the
    # content; this serializer only reads the URL.

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Ensure only trainers can use this serializer
        if self.instance and not self.instance.is_trainer:
            raise serializers.ValidationError(_("This serializer is only for trainers"))
        return attrs

class ClientProfileSerializer(OnboardingFieldsMixin, serializers.ModelSerializer):
    """Serializer for client-specific profile data, including the v2 onboarding
    answers and the date of birth (see OnboardingFieldsMixin)."""
    assigned_trainer_name = serializers.SerializerMethodField()
    profile_picture = serializers.SerializerMethodField()
    
    class Meta:
        model = CustomUser
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 'profile_picture',
            'height', 'weight', 'age', 'gender', 'specific_injury',
            'activity_level', 'assigned_trainer', 'assigned_trainer_name',
            'client_goals', 'client_preferences',
            'date_of_birth', *CLIENT_ONBOARDING_FIELDS, 'onboarding_missing',
        ]
        # `assigned_trainer` is READ-ONLY. It was writable here, so a client could
        # PATCH their own profile and attach any trainer to themselves — no request,
        # no approval, no charge — and every gate that still reads the pointer then
        # treated that stranger as their trainer. Setting it to null while an approved
        # relation held their money stranded the payment. The bond is created only by
        # the request/approve pair.
        read_only_fields = ['id', 'username', 'email', 'assigned_trainer']

    def get_assigned_trainer_name(self, obj):
        if obj.assigned_trainer:
            return obj.assigned_trainer.full_name
        return None

    def get_profile_picture(self, obj):
        request = self.context.get('request')
        if obj.profile_picture:
            url = obj.profile_picture.url
            if request is not None:
                return request.build_absolute_uri(url)
            return url
        return None

    # No `validate_profile_picture`: `profile_picture` is a read-only method field, so
    # one here never ran (SIDE_BUGS S-150, as B-026 for UserDetailsSerializer).
    # Pictures go through `POST /api/auth/user/profile-picture/`, which checks the
    # content; this serializer only reads the URL.

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Ensure only clients can use this serializer
        if self.instance and not self.instance.is_client:
            raise serializers.ValidationError(_("This serializer is only for clients"))
        return attrs

class CustomUserSerializer(serializers.ModelSerializer):
    profile_picture = serializers.SerializerMethodField()
    class Meta:
        model = CustomUser
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 'profile_picture',
            'phone_number', 'date_joined', 'is_active', 'is_staff', 'is_superuser', 'user_type'
        ]
        read_only_fields = [
            'id', 'date_joined', 'is_active', 'is_staff', 'is_superuser', 'user_type'
        ]    

    def get_profile_picture(self, obj):
        request = self.context.get('request')
        if obj.profile_picture:
            url = obj.profile_picture.url
            if request is not None:
                return request.build_absolute_uri(url)
            return url
        return None

    # No `validate_profile_picture`: `profile_picture` is a read-only method field, so
    # one here never ran (SIDE_BUGS S-150, as B-026 for UserDetailsSerializer).
    # Pictures go through `POST /api/auth/user/profile-picture/`, which checks the
    # content; this serializer only reads the URL.

class DeviceTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceToken
        fields = ['id', 'user', 'token', 'created_at', 'updated_at']
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']    


# ============================================================================
# PASSWORD RESET SERIALIZERS
# ============================================================================

class PasswordResetRequestSerializer(serializers.Serializer):
    """Serializer for requesting a password reset OTP."""
    email = serializers.EmailField(required=True)


class PasswordResetVerifySerializer(serializers.Serializer):
    """Serializer for verifying the password reset OTP."""
    email = serializers.EmailField(required=True)
    otp_code = serializers.CharField(max_length=6, min_length=6, required=True)

    def validate_otp_code(self, value):
        from .utils import western_digits
        value = western_digits(value)
        if not re.fullmatch(r'[0-9]{6}', value):
            raise serializers.ValidationError(_("OTP code must be a 6-digit number."))
        return value


class PasswordResetConfirmSerializer(serializers.Serializer):
    """Serializer for setting a new password using the reset token."""
    # A url-safe random string now, not a UUID: the raw token is generated by
    # PasswordResetToken.issue() and only its keyed hash is stored.
    reset_token = serializers.CharField(required=True, max_length=128)
    # No `min_length` here: it refused a short password with code `min_length` before
    # the Django validators ran, while registration refuses the same rule as
    # `password_too_short` — two codes for one rule (BACKEND_BUGS B-034). The validators
    # (AUTH_PASSWORD_VALIDATORS, minimum 8) decide, with their own codes.
    new_password = serializers.CharField(required=True, write_only=True)

    def validate_new_password(self, value):
        from django.contrib.auth.password_validation import validate_password
        validate_password(value)
        return value