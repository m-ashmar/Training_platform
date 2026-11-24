from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.utils.html import format_html
from django.urls import reverse
from django.http import HttpResponseRedirect
from .models import CustomUser
from training_platform.admin_utils import bulk_apply
import logging

logger = logging.getLogger(__name__)

class ClientInline(admin.TabularInline):
    """A trainer's clients, for READING.

    The inline's object is the client's USER ACCOUNT, not the relation between them. With
    Django's default `can_delete = True`, the DELETE box on a row reads as "remove this
    client from this trainer" and deleted the person: the account, its training history,
    its goals and its analytics cascaded away, behind a confirmation that just said
    "delete". Only users who happened to own a wallet were saved, by `Wallet.owner` being
    PROTECT — 7 of 510 in the measured database. Adding a row created a brand-new user
    pointed at the trainer, with no relation, no consent and no escrow.

    Relations are changed through the relation itself; this only shows who they are.
    """

    model = CustomUser
    fk_name = 'assigned_trainer'
    fields = ('username', 'email', 'height', 'weight', 'age', 'activity_level')
    readonly_fields = fields
    extra = 0
    can_delete = False
    verbose_name = 'Client'
    verbose_name_plural = 'Clients'
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

# Each goes through `CustomUser.save()` and writes an admin log entry per row.
# `queryset.update()` skipped both: activating a `user_type='admin'` account in bulk never
# gave it `is_staff`, so it could not reach the admin. See training_platform/admin_utils.py.
#
# There is no "mark as verified" action any more, and `trainer_is_verified` is read-only
# on the form below. A trainer is verified by an operator deciding their application —
# the documents they sent, looked at — in the verification queue
# (trainer_verification/admin.py), and that decision is the ONE place the flag is written
# (`trainer_verification.services.decide` / `revoke`). One tick in a bulk action verified
# people nobody had checked, with no documents and no record of what was looked at.
@admin.action(description="Mark selected trainers as available", permissions=["change"])
def make_available(modeladmin, request, queryset):
    count = bulk_apply(request, queryset.filter(user_type='trainer'),
                       "Trainer marked available via bulk admin action",
                       trainer_is_available=True)
    messages.success(request, f"Marked {count} trainers as available.")

# Every action declares the permission it needs (Road to 10 round 4, 2026-11-24). An
# action with none is offered by Django to anyone who can VIEW the changelist: a staff
# account given only `view_customuser` deactivated accounts and took pictures down.
# `tests/test_road10_admin_action_permissions.py` walks every ModelAdmin of both sites.
#
# allauth's `EmailAddressAdmin.make_verified` (registered on the stock `/admin/`) marks
# addresses verified and declares nothing: it needs the change permission too.
from allauth.account.admin import EmailAddressAdmin as _EmailAddressAdmin  # noqa: E402

_EmailAddressAdmin.make_verified.allowed_permissions = ('change',)


# The bulk "reset to a known password" action is gone. It set every selected account
# to one shared, source-visible string, which is an account takeover of every user an
# operator can select. Password recovery goes through the OTP flow like everyone else.
@admin.action(description="Remove the profile picture of the selected users", permissions=["change"])
def remove_profile_pictures(modeladmin, request, queryset):
    """A reported picture taken down (the owner's D9, 2026-11-22: a trainer uploads their
    own photo; it can be reported, `target_type: user`, and removed). The file leaves
    storage through the replace/delete cleanup (`training_platform/signals.py`); the
    person may upload another, which is checked like the first."""
    count = bulk_apply(request, queryset.exclude(profile_picture='')
                       .exclude(profile_picture__isnull=True),
                       "Profile picture removed via admin action", profile_picture=None)
    messages.success(request, f"Removed {count} profile pictures.")


@admin.action(description="Activate selected users", permissions=["change"])
def activate_users(modeladmin, request, queryset):
    count = bulk_apply(request, queryset, "Activated via bulk admin action", is_active=True)
    messages.success(request, f"Activated {count} users.")

@admin.action(description="Deactivate selected users", permissions=["change"])
def deactivate_users(modeladmin, request, queryset):
    count = bulk_apply(request, queryset, "Deactivated via bulk admin action", is_active=False)
    messages.success(request, f"Deactivated {count} users.")

def coaching_in_the_way(client, new_trainer_id):
    """The approved coaching an operator's change of `assigned_trainer` would end, or
    None.

    The console syncs the relation to the pointer, and it used to do so by rejecting the
    client's other approved coaching with a bare UPDATE: no `release_hold`, no
    `end_coaching`, so a month held in escrow stayed there on a rejected row. Pointing a
    client at a second trainer raised `IntegrityError` first (one approved coaching per
    client), a 500; clearing the pointer did reject the coaching, around the money. A
    coaching ends through the paths that settle its money: the trainer's
    `trainer/unassign-client/`, the client's `client/unassign-trainer/`, or the renewal
    run (the phase-3 review's finding, 2026-11-24). `new_trainer_id` None is clearing
    the pointer, which every approved coaching is in the way of.
    """
    from .models import TrainerClientRelation

    return (TrainerClientRelation.objects.filter(client=client, status='approved')
            .exclude(trainer_id=new_trainer_id).select_related('trainer').first())


def coaching_in_the_way_message(relation):
    from django.utils.translation import gettext

    trainer = relation.trainer
    return gettext(
        "This client has a running coaching with %(name)s. To change trainer, end it "
        "first: the trainer or the client ends it in the app, which returns any money "
        "held for it. To put the trainer field back in step, choose %(name)s.") % {
            'name': trainer.full_name or trainer.username}


def no_coaching_to_point_at(client, trainer_id, relations=None):
    """The trainer a change of `assigned_trainer` names when the client has NO approved
    coaching with them, or None.

    The owner (2026-11-24): the console must not be able to approve a coaching with no
    consent and no charge. The pointer's sync used to `update_or_create` the relation as
    `approved` — a pending request, a rejected one, or none at all — so an operator's
    pick started a coaching the client never agreed to and no month was ever taken for.
    A coaching starts only in the app (`client/request-trainer/`, then the trainer's
    `respond-to-request/`, which takes month 1 when the trainer charges: users/
    trainer_relations.py). The console only puts the pointer back in step with one that
    is running. `relations` is the client's rows keyed by trainer id, when the caller
    has them locked; otherwise they are read.
    """
    from .models import CustomUser, TrainerClientRelation

    if not trainer_id:
        return None
    if relations is not None:
        row = relations.get(trainer_id)
        running = row is not None and row.status == 'approved'
    else:
        running = TrainerClientRelation.objects.filter(
            client=client, trainer_id=trainer_id, status='approved').exists()
    if running:
        return None
    return CustomUser.objects.filter(pk=trainer_id).first()


def no_coaching_message(trainer):
    from django.utils.translation import gettext

    return gettext(
        "This client has no running coaching with %(name)s, and the console cannot "
        "start one: a coaching begins in the app, when the client asks and the trainer "
        "accepts, which takes the first month if the trainer charges. Here you can only "
        "choose the trainer of the client's running coaching, or leave the field "
        "empty.") % {'name': (trainer.full_name or trainer.username) if trainer else '-'}


class CustomUserChangeForm(UserChangeForm):
    """Custom form for changing user data in admin"""
    class Meta(UserChangeForm.Meta):
        model = CustomUser
        fields = '__all__'

    def clean(self):
        """A change of `assigned_trainer` that would end another approved coaching, or
        that names a trainer the client has no approved coaching with, is a field error
        on `assigned_trainer`, so the operator is told and nothing is written
        (`coaching_in_the_way`, `no_coaching_to_point_at`). `save_model` checks again
        under the lock."""
        cleaned = super().clean()
        if self.instance.pk and 'assigned_trainer' in self.changed_data:
            new = cleaned.get('assigned_trainer')
            other = coaching_in_the_way(self.instance, new.pk if new else None)
            if other is not None:
                self.add_error('assigned_trainer', coaching_in_the_way_message(other))
            elif no_coaching_to_point_at(self.instance, new.pk if new else None):
                self.add_error('assigned_trainer', no_coaching_message(new))
        return cleaned

class CustomUserCreationForm(UserCreationForm):
    """Custom form for creating users in admin"""
    class Meta(UserCreationForm.Meta):
        model = CustomUser
        fields = ('email', 'username', 'phone_number', 'user_type')

@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm
    
    list_display = (
        'username', 'email', 'user_type', 'is_active', 'is_staff',
        'assigned_trainer', 'trainer_is_verified', 'trainer_is_available',
        'date_joined', 'password_status'
    )
    # Not `is_staff` and not `user_type`. Either is a privilege grant, and from the
    # changelist it was one checkbox with no confirmation and no change message: one
    # click turned a trainer into a reader of every client's analytics, because the
    # analytics permissions are `is_staff`. Grant roles from the change form.
    list_editable = ('is_active', 'trainer_is_available')
    # `groups`: how an operator finds the store-review demo accounts
    # (`seed_review_accounts` puts them in one group; nothing else reads it).
    list_filter = (
        'user_type', 'is_active', 'is_staff', 'trainer_is_verified',
        'trainer_is_available', 'date_joined', 'groups'
    )
    search_fields = ('username', 'email', 'phone_number', 'first_name', 'last_name')
    ordering = ('-date_joined',)
    inlines = [ClientInline]
    
    actions = [
        make_available,
        activate_users,
        deactivate_users,
        remove_profile_pictures,
    ]
    
    fieldsets = (
        (None, {
            'fields': ('username', 'email', 'phone_number', 'password')
        }),
        ('Personal info', {
            'fields': (
                'first_name', 'last_name', 'profile_picture', 'date_of_birth', 'height',
                'weight', 'age', 'gender', 'activity_level', 'specific_injury'
            )
        }),
        ('Trainer info', {
            'fields': (
                'trainer_bio', 'trainer_specializations', 'trainer_certifications', 
                'trainer_experience_years', 'trainer_hourly_rate', 
                'trainer_is_verified', 'trainer_is_available',
                # Matching codes, e.g. ["strength", "build_muscle"].
                'trainer_goal_focus', 'trainer_injury_focus', 'trainer_training_places',
            ),
            'classes': ('collapse',)
        }),
        ('Client info', {
            'fields': ('assigned_trainer', 'client_goals', 'client_preferences',
                       'training_goal', 'training_place', 'training_days_per_week',
                       'injury_areas'),
            'description': ('The trainer here can only be the trainer of the client\'s '
                            'running coaching, or empty. A coaching starts in the app '
                            '(the client asks, the trainer accepts and month 1 is '
                            'taken); the console never starts or ends one.'),
            'classes': ('collapse',)
        }),
        ('Permissions', {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')
        }),
        ('Important dates', {
            'fields': ('last_login', 'date_joined'),
            'classes': ('collapse',)
        }),
    )
    
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'email', 'phone_number', 'user_type', 'password1', 'password2'),
        }),
    )
    
    # `trainer_is_verified`: changed only by a decision in the verification queue.
    readonly_fields = ('date_joined', 'last_login', 'password_status', 'trainer_is_verified')
    
    def password_status(self, obj):
        """Display password status in admin list"""
        # Ask the model, not the hash prefix: `startswith('pbkdf2')` labels every account
        # "Not Set" the day the hasher changes to argon2 or bcrypt.
        # The label is an argument: format_html() with none is deprecated since Django
        # 5.0 and a TypeError in 6.0.
        if obj.password and obj.has_usable_password():
            return format_html('<span style="color: green;">{}</span>', '✓ Set')
        else:
            return format_html('<span style="color: red;">{}</span>', '✗ Not Set')
    password_status.short_description = 'Password Status'

    def has_delete_permission(self, request, obj=None):
        """No hard delete from either admin site. `Wallet.owner` and
        `CoachingPeriod.trainer` are PROTECT, so it failed for most accounts anyway, and
        where it did not (a trainer with no wallet) it cascaded their clients' routines
        away and skipped the owner's rule that a trainer with active clients is never
        deleted. `CustomUser.retire()` is the one erasure path; suspend with `is_active`."""
        return False

    def save_model(self, request, obj, form, change):
        """Hash a changed password, and keep the pointer in step with the coaching.

        `assigned_trainer` is editable here, and every authorization check in the diet
        app requires BOTH the pointer and an approved `TrainerClientRelation`. The
        console may only put a stale pointer back in step with the coaching that is
        running, or clear a pointer no coaching stands behind. It writes no relation:

        * A change that would END another approved coaching (a second trainer, or the
          pointer cleared) is refused: the console cannot settle that coaching's money
          (`coaching_in_the_way`).
        * A trainer the client has NO approved coaching with — a pending request, a
          rejected one, or none — is refused (`no_coaching_to_point_at`; the owner,
          2026-11-24). This saved the relation as `approved`, so an operator's pick
          started a coaching with no consent from the client and no month taken.
        * The trainer of the coaching already running: only the pointer is written. The
          relation is left exactly as it is — its author, its note, the client's «do not
          renew» and the trainer's «end when this month ends» (S2.7, SIDE_BUGS S-264)
          are the coaching's, not the console's.

        Refused means `ValidationError` and nothing written; the form refuses the same
        changes first (`CustomUserChangeForm.clean`), this is the re-check under the
        lock for a coaching that started or ended since.
        """
        from django.core.exceptions import ValidationError
        from django.db import transaction

        from .models import TrainerClientRelation

        if change and 'password' in form.changed_data:
            if obj.password and not obj.password.startswith('pbkdf2'):
                obj.password = make_password(obj.password)
        if 'assigned_trainer' not in getattr(form, 'changed_data', []):
            super().save_model(request, obj, form, change)
            return
        with transaction.atomic():
            # The client's relations FIRST, then the user row (the lock order of
            # users/trainer_relations.py: relation, then the user rows): every row of
            # the client's is locked before anything is read or written, so two saves
            # at once cannot both read "no coaching yet" and keep a stale end, and a
            # renewal holding the relation is waited for rather than deadlocked.
            # In id order, the order `release_holds_before_erasure` takes them.
            rows = {row.trainer_id: row for row in
                    TrainerClientRelation.objects.select_for_update()
                    .filter(client_id=obj.pk).order_by('pk')}
            in_the_way = next((row for row in rows.values() if row.status == 'approved'
                               and row.trainer_id != obj.assigned_trainer_id), None)
            if in_the_way is not None:
                # The form refused it already (`CustomUserChangeForm.clean`); this is
                # the same answer for a coaching approved since the form was checked.
                raise ValidationError(coaching_in_the_way_message(
                    TrainerClientRelation.objects.select_related('trainer')
                    .get(pk=in_the_way.pk)))
            nobody_agreed = no_coaching_to_point_at(obj, obj.assigned_trainer_id,
                                                    relations=rows)
            if nobody_agreed is not None:
                # No consent and no charge: the console never starts a coaching (the
                # owner, 2026-11-24). Read from the locked rows, so a coaching that
                # ended since the form was checked is refused too.
                raise ValidationError(no_coaching_message(nobody_agreed))
            # The pointer only: cleared with no coaching behind it, or put back in step
            # with the one running. The relation is not written.
            super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        """Optimize queryset with select_related"""
        return super().get_queryset(request).select_related('assigned_trainer')
    

    
    def response_change(self, request, obj):
        """Custom response after user change.

        The reset button set the account to one shared, source-visible password and
        told the operator what it was. Anyone who read this file could sign in as any
        account an operator had ever "helped". An account is recovered through the OTP
        flow, which is the only path that proves the person owns the address.

        It also ends every session the account has (Road to 10 round 4): an operator
        resets a password because the account may be in someone else's hands, and
        leaving its refresh tokens alive kept every signed-in device signed in for up
        to seven days. The same revocation the password change makes
        (`users/utils.py end_every_session`), in the same transaction as the reset.
        """
        if '_reset_password' in request.POST:
            from django.db import transaction

            from .utils import end_every_session

            with transaction.atomic():
                obj.set_unusable_password()
                obj.save(update_fields=['password'])
                ended = end_every_session(obj)
            messages.success(
                request,
                f"{obj.email} can no longer sign in with a password, and was signed out "
                f"of {ended} session(s). Ask them to use 'forgot password' to set a new one."
            )
            return HttpResponseRedirect('.')
        return super().response_change(request, obj)
    
    def change_view(self, request, object_id, form_url='', extra_context=None):
        """Add custom buttons to change view"""
        extra_context = extra_context or {}
        extra_context['show_reset_password'] = True
        return super().change_view(request, object_id, form_url, extra_context)

# Register other models if they exist
try:
    from .models import TrainerClientRelation, DeviceToken
    
    @admin.register(TrainerClientRelation)
    class TrainerClientRelationAdmin(admin.ModelAdmin):
        """Read-only, as the operator console's is (admin_dashboard/admin.py).

        This row carries consent, the agreed price, the escrow reference and both ends
        of the coaching. With every field editable here, any staff account could set
        `status='approved'` with no client consent and no money held, point
        `hold_reference` at nothing, change `agreed_rate`, or set an end flag (the only
        source of SIDE_BUGS S-266's state); deleting a row left its held month in escrow
        with nothing to return it from (`CoachingPeriod.relation` is SET_NULL). A
        relation's money moves only through users/trainer_relations.py and
        users/coaching.py: the request, the answers, the first programme, the monthly
        run and the two leaving routes. No console path writes a relation: the user
        form's `assigned_trainer` (`CustomUserAdmin.save_model`) only puts a stale
        pointer back in step with a running coaching, and refuses anything that would
        end one (the phase-3 review, round 4) or start one with no consent and no
        charge (the owner, 2026-11-24).
        """
        list_display = ('trainer', 'client', 'status', 'initiated_by', 'agreed_rate',
                        'hold_reference', 'created_at')
        list_filter = ('status', 'created_at')
        search_fields = ('trainer__email', 'client__email')
        readonly_fields = ('trainer', 'client', 'status', 'initiated_by', 'agreed_rate',
                           'agreed_currency', 'hold_reference', 'requested_at',
                           'ends_at_period_end', 'client_stops_renewal', 'client_note',
                           'responded_at', 'created_at', 'updated_at')

        def has_add_permission(self, request):
            return False

        def has_change_permission(self, request, obj=None):
            return False

        def has_delete_permission(self, request, obj=None):
            return False
        
    @admin.register(DeviceToken)
    class DeviceTokenAdmin(admin.ModelAdmin):
        list_display = ('user', 'token', 'platform', 'last_used_at', 'is_active')
        list_filter = ('platform', 'is_active', 'last_used_at')
        search_fields = ('user__email', 'token')
        readonly_fields = ('created_at', 'last_used_at')
        
except ImportError:
    # Optional side effect: swallowing this silently is what made the
    # surrounding failures invisible in logs. Control flow is unchanged.
    logger.debug('suppressed non-fatal error', exc_info=True)