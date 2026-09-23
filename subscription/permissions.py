from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions

#: Why a subscriber was refused, as the stable codes the app branches on.
#:
#: These permissions returned a bare False, so DRF answered every refusal with its
#: generic "You do not have permission to perform this action." / `permission_denied`.
#: A lapsed client and a client whose plan lacks a feature got the same body as an
#: authorization error, the app could not show "renew your subscription", and the
#: `no_entitlement` responses written inside the diet views were unreachable because
#: this check runs first.
NO_ENTITLEMENT = ("no_entitlement", _("An active subscription is needed for this feature."))
NOT_IN_PLAN = ("feature_not_in_plan", _("Your plan does not include this feature."))

#: A trainer is exempt from the paywall only once a human has VERIFIED them.
#:
#: `user_type='trainer'` is self-service at registration (users/serializers.py accepts
#: it; only `admin` and `agent` are blocked), and both permissions below short-circuited
#: on `is_trainer` alone — a role check standing in for an entitlement check. So anyone
#: who picked "trainer" at signup got the whole paid product for free, permanently, with
#: no admin ever approving them, while the people who told the truth about who they are
#: paid for it.
#:
#: `trainer_is_verified` already exists, is writable only from the admin, and gated
#: nothing anywhere: a grep found it in list displays and response payloads and in no
#: permission class. This is the check it was made for.
def _is_exempt_from_paywall(user) -> bool:
    """Staff and admins always; trainers once verified."""
    if getattr(user, 'is_admin', False) or user.is_staff:
        return True
    return bool(getattr(user, 'is_trainer', False)
                and getattr(user, 'trainer_is_verified', False))


class HasActiveSubscription(permissions.BasePermission):
    """
    Permission to check if user has an active subscription.
    """
    code, message = NO_ENTITLEMENT

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False

        # Staff and trainers are not subscribers. This read `request.user.subscription`
        # for everyone, so a trainer or admin with no subscription row was refused on
        # every trainer endpoint — including the ones deliberately widened to admit
        # them, which made the widening unreachable.
        user = request.user
        if _is_exempt_from_paywall(user):
            return True

        # Only "has no subscription" is a refusal. A catch-all turned a database error
        # into a 403 telling a paying subscriber they had no subscription.
        try:
            subscription = request.user.subscription
        except ObjectDoesNotExist:
            return False
        return subscription.is_active


class HasSubscriptionFeature(permissions.BasePermission):
    """
    Permission to check if user's subscription includes specific features.
    """
    required_feature = None
    code, message = NO_ENTITLEMENT

    def __init__(self, required_feature=None):
        if required_feature is not None:
            self.required_feature = required_feature
        super().__init__()

    def has_permission(self, request, view):
        # Staff, trainers and admins are not subscribers. `HasDietAccess` delegates
        # here, so without this every trainer diet endpoint was refused before its own
        # `is_trainer or is_admin` check could run — the four trainer routes were dead.
        # The bypass existed only on `HasActiveSubscription`, which these do not use.
        user = request.user
        if not user.is_authenticated:
            return False
        if _is_exempt_from_paywall(user):
            return True

        try:
            subscription = request.user.subscription
        except ObjectDoesNotExist:
            self.code, self.message = NO_ENTITLEMENT
            return False

        if not subscription.is_active:
            self.code, self.message = NO_ENTITLEMENT
            return False

        flag = {
            'diet': 'has_diet_access',
            'routine': 'has_routine_access',
            'challenges': 'has_challenges_access',
            'ai_advice': 'has_ai_advice',
            'priority_support': 'has_priority_support',
        }.get(self.required_feature)
        if flag is None or getattr(subscription, flag):
            return True
        self.code, self.message = NOT_IN_PLAN
        return False


# Subclasses rather than wrappers that built a fresh HasSubscriptionFeature: DRF reads
# `code` and `message` off the instance it holds, so a wrapper's refusal reason was lost.
class HasDietAccess(HasSubscriptionFeature):
    """Permission to check if user has diet feature access."""
    required_feature = 'diet'


class HasDietAccessOrFrozenRead(HasDietAccess):
    """Diet access, or READ access to your own plans after the subscription lapsed.

    The owner (2026-09-19): after the plan ends the client still sees their last diet
    plan and cannot use it — no generating, ticking, swapping, rating, advice or food
    catalogue — until they subscribe again. So a GET passes for a frozen row
    (`Subscription.is_frozen`) whose plan has the diet feature, or whose last run of
    PAID cover included a plan that did (`Subscription.last_run_plans` — the row may have
    been moved onto another plan since, which buys nothing); every other refusal is
    unchanged (`no_entitlement`, `feature_not_in_plan`), and the app tells "ended" from
    "never subscribed" by `access` on `GET subscriptions/current/`.

    Only on views that read the caller's own plans and progress. Never on a route in
    CACHEABLE_ROUTES: the private cache key carries no entitlement.
    """

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True
        if request.method not in permissions.SAFE_METHODS:
            return False
        try:
            subscription = request.user.subscription
        except (ObjectDoesNotExist, AttributeError):
            return False
        if not subscription.is_frozen:
            return False
        if subscription.has_diet_access:
            return True
        # What was PAID for decides the frozen read, not only the plan the row is on
        # now: `change-plan` moves an unpaid row onto another plan and its flags, and
        # choosing a plan without diet — buying nothing — took the last diet plan away.
        return any(plan.has_diet_access for plan in subscription.last_run_plans())


class HasRoutineAccess(HasSubscriptionFeature):
    """Permission to check if user has routine feature access."""
    required_feature = 'routine'


class HasChallengesAccess(HasSubscriptionFeature):
    """Permission to check if user has challenges feature access."""
    required_feature = 'challenges'


class HasAIAdviceAccess(HasSubscriptionFeature):
    """Permission to check if user has AI advice feature access."""
    required_feature = 'ai_advice'


class HasPrioritySupportAccess(HasSubscriptionFeature):
    """Permission to check if user has priority support access."""
    required_feature = 'priority_support'


# The three metering permission classes that stood here — `SubscriptionUsageLimit`,
# `MealUsageLimit`, `RoutineUsageLimit` — were wired to nothing. Metering is enforced
# where the work happens: `diet/views.py` calls `subscription.quota.reserve(...)` inside
# the same atomic act as the generation, because a permission class can only READ the
# counter and every concurrent request then passed the same check. A permission class
# that duplicates that decision is a second answer waiting to disagree with the first,
# and one of them did: it kept its own copy of the lookup and inserted duplicate rows
# that locked the subscriber out. `subscription.quota.FEATURES` is the authority.
