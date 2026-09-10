"""One owner for "has this subscriber any of that feature left, and record that they used some".

Before this module the answer was split across three places that did not agree:

* `SubscriptionUsageLimit.has_permission` read a usage row and compared its count to a
  plan limit, but looked the row up on `(subscription, feature)` while the table's
  unique key was `(subscription, feature, period_start)` — and `period_start` was
  `auto_now_add`, so no two rows ever collided and the constraint could not fire.
  Concurrent requests each inserted their own row; from the second row on the lookup
  raised `MultipleObjectsReturned`, a bare `except:` turned that into a denial, and the
  paying subscriber was locked out of diet and meal generation for good.
* `track_feature_usage` held the only increment, resolved the limit from an attribute
  the plan does not have (`max_daily_meals` for the `daily_meals` feature, where the
  field is `max_meals_per_day`), and was called from nowhere. `usage_count` therefore
  stayed at 0 and every limit passed.
* The permission class created `SubscriptionFeature` rows as a side effect of a read.

So: features are declared here, the period each one counts over is a computed boundary
rather than "whenever the row happened to be written", and the check and the increment
are two functions over the same row.
"""
import logging

from django.db import models, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: feature name -> (field on SubscriptionPlan holding the limit, period granularity).
#: A limit of 0 on the plan means unlimited, which is how the plans already read.
FEATURES = {
    "daily_meals": ("max_meals_per_day", "day"),
    "routines": ("max_routines", "subscription"),
}


class QuotaUnavailable(Exception):
    """The quota could not be evaluated, so no claim about it can be made."""


def _period(subscription, granularity, now):
    """The window this feature counts over, as a (start, end) pair.

    Deterministic by construction: every request inside the same window computes the
    same start, which is what lets the unique constraint do its job and what makes
    `get_or_create` race-safe.
    """
    if granularity == "day":
        import datetime as _dt

        from training_platform.user_time import today_for

        # The CLIENT's calendar DATE, pinned to UTC — not the UTC instant of their local
        # midnight. `timezone.localtime` resolves against TIME_ZONE (Asia/Damascus) for
        # everyone, so a client in Berlin or the Gulf had their daily allowance reset at
        # a random hour of their own afternoon; that part was right and stays.
        #
        # What was wrong is that the row's unique key then contained a value the user can
        # WRITE. `SubscriptionUsage` is unique on (subscription, feature, period_start),
        # and `period_start` was the instant of local midnight — a different instant for
        # every UTC offset — so `POST /api/auth/user/update/ {"preferred_timezone": ...}`
        # between generations minted a fresh row with `usage_count = 0`. Measured: a
        # 1/day plan yielded 11 extra allowances from 12 timezones, and there are about
        # 38 distinct offsets.
        #
        # Keying on the local DATE instead means every timezone that shares today shares
        # the row. Moving timezone can still shift a user by at most one calendar day,
        # which is what genuinely travelling does, rather than handing out an allowance
        # per offset.
        local_date = today_for(getattr(subscription, "user", None), now)
        start = _dt.datetime.combine(
            local_date, _dt.time.min, tzinfo=_dt.timezone.utc)
        return start, start + timezone.timedelta(days=1)
    # "subscription": the current paid period, so a limit like max_routines resets
    # when the customer renews rather than on an arbitrary rolling date.
    start = subscription.start_date or now
    end = subscription.end_date or (now + timezone.timedelta(days=30))
    if end <= start:
        end = start + timezone.timedelta(days=30)
    return start, end


def _row_for(user, feature_name, *, now=None):
    """The usage row for this user's current window, plus the plan's limit.

    Returns `(usage, limit)`. `limit` of 0 means unlimited.
    """
    from subscription.models import Subscription, SubscriptionFeature, SubscriptionUsage

    try:
        limit_field, granularity = FEATURES[feature_name]
    except KeyError:
        raise QuotaUnavailable(f"{feature_name!r} is not a declared quota feature")

    try:
        subscription = user.subscription
    except (AttributeError, Subscription.DoesNotExist):
        raise QuotaUnavailable("user has no subscription")
    if not subscription.is_active:
        raise QuotaUnavailable("subscription is not active")

    limit = getattr(subscription.plan, limit_field, 0) or 0

    try:
        feature = SubscriptionFeature.objects.get(name=feature_name)
    except SubscriptionFeature.DoesNotExist:
        # Declared in FEATURES and seeded by migration 0008. A read path must not
        # invent one; if it is missing that is a deployment fault worth seeing.
        raise QuotaUnavailable(f"feature row {feature_name!r} is missing")

    now = now or timezone.now()
    start, end = _period(subscription, granularity, now)
    usage, _created = SubscriptionUsage.objects.get_or_create(
        subscription=subscription,
        feature=feature,
        period_start=start,
        defaults={"limit": limit, "period_end": end},
    )
    return usage, limit


def has_headroom(user, feature_name) -> bool:
    """Whether `user` may use `feature_name` once more right now.

    Any failure to evaluate is a denial, but a logged one: the silent version of this
    is what hid the lockout.
    """
    try:
        usage, limit = _row_for(user, feature_name)
    except QuotaUnavailable as exc:
        logger.info("quota check denied for user %s on %s: %s",
                    getattr(user, "id", None), feature_name, exc)
        return False
    except Exception:
        logger.exception("quota check failed for user %s on %s",
                         getattr(user, "id", None), feature_name)
        return False
    return limit == 0 or usage.usage_count < limit


def consume(user, feature_name, amount: int = 1) -> bool:
    """Record `amount` uses of `feature_name`. Returns whether it was recorded.

    Incremented with `F()` so parallel calls add up instead of overwriting each other.
    Call this after the work succeeded; a refused request must not spend quota.
    """
    try:
        usage, _limit = _row_for(user, feature_name)
    except QuotaUnavailable as exc:
        logger.info("not recording %s for user %s: %s",
                    feature_name, getattr(user, "id", None), exc)
        return False
    except Exception:
        logger.exception("could not record %s for user %s",
                         feature_name, getattr(user, "id", None))
        return False

    from subscription.models import SubscriptionUsage

    SubscriptionUsage.objects.filter(pk=usage.pk).update(
        usage_count=models.F("usage_count") + amount
    )
    return True


def record_on_commit(user, feature_name, amount: int = 1) -> None:
    """Spend quota once the surrounding work is committed, and not if it rolls back."""
    transaction.on_commit(lambda: consume(user, feature_name, amount))


class NoEntitlement(Exception):
    """There is no allowance to claim, because there is no subscription to claim it from."""


def reserve(user, feature_name, amount: int = 1) -> bool:
    """Claim `amount` of the allowance now, atomically. Returns whether it was claimed.

    Raises `NoEntitlement` when the caller has no subscription or an inactive one — which
    is a different answer from "you have used today's allowance" and deserves a different
    status. An admin, and a client whose subscription lapsed between the permission check
    and here, were both told to come back tomorrow for a limit that does not apply to
    them.

    `has_headroom` then work then `consume` is check-then-act across two transactions,
    and the gap is the whole request. Twenty parallel taps on Generate by a client whose
    plan allows one meal a day all read `usage_count = 0`, all passed, and all enqueued a
    thirty-day build — about 120 optimiser runs each — because nothing was written until
    the last of them finished. The plan model supersedes rather than refuses, so not one
    of them even errored.

    Locking the row and incrementing inside the same transaction makes the check and the
    spend one act, so the second caller reads the first caller's increment. Work that
    then fails calls `release`.
    """
    from django.db import transaction as _txn
    from subscription.models import SubscriptionUsage

    try:
        with _txn.atomic():
            usage, limit = _row_for(user, feature_name)
            locked = (SubscriptionUsage.objects.select_for_update()
                      .filter(pk=usage.pk).first())
            if locked is None:
                return False
            if limit and locked.usage_count + amount > limit:
                return False
            locked.usage_count = locked.usage_count + amount
            locked.save(update_fields=["usage_count"])
            return True
    except QuotaUnavailable as exc:
        logger.info("quota reservation denied for user %s on %s: %s",
                    getattr(user, "id", None), feature_name, exc)
        raise NoEntitlement(str(exc)) from exc
    except Exception:
        logger.exception("quota reservation failed for user %s on %s",
                         getattr(user, "id", None), feature_name)
        return False


def release(user, feature_name, amount: int = 1) -> None:
    """Give back an allowance claimed by `reserve` for work that did not happen.

    Never drops below zero: a release without a matching reserve must not hand the
    client a free extra.
    """
    from django.db.models import Case, F, Value, When
    from subscription.models import SubscriptionUsage

    try:
        usage, _limit = _row_for(user, feature_name)
    except Exception:
        logger.exception("could not release %s for user %s",
                         feature_name, getattr(user, "id", None))
        return
    SubscriptionUsage.objects.filter(pk=usage.pk).update(
        usage_count=Case(
            When(usage_count__gte=amount, then=F("usage_count") - amount),
            default=Value(0),
        )
    )


def release_on_commit(user, feature_name, amount: int = 1) -> None:
    transaction.on_commit(lambda: release(user, feature_name, amount))
