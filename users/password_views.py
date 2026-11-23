"""Changing the password while signed in (Road to 10 S2.8; the owner's D14: before launch).

Only the forgot-password flow existed (`users/views.py PasswordResetConfirmView`), so a
person who knew their password and wanted another had to pretend they had lost it.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils.translation import gettext as _
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

#: Wrong current passwords one account may send before it is made to wait: a stolen
#: access token must not become a way to guess the password and lock the owner out.
MAX_WRONG_CURRENT = 5
WRONG_CURRENT_WINDOW_SECONDS = 15 * 60


class PasswordChangeSerializer(serializers.Serializer):
    # Trimmed, as every other password field is: sign-up, `login/`, `token/` and the
    # reset all drop the spaces around a password (DRF's default). Kept here, a new
    # password typed with a trailing space was stored with it, and no sign-in could ever
    # match it again — the trimmed string is not the stored one, and the untrimmed one
    # is trimmed on the way in.
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_new_password(self, value):
        # The sign-up rules and their codes (`password_too_short`, `password_too_common`,
        # `password_entirely_numeric`, `password_too_similar`), as the reset answers them.
        from django.contrib.auth.password_validation import validate_password

        validate_password(value, user=self.context.get('user'))
        return value


def _wrong_key(user_id) -> str:
    return f"password_change_wrong:{user_id}"


def _wrong_count(user_id) -> int:
    from training_platform.cache import ratelimit_cache

    try:
        return int(ratelimit_cache().get(_wrong_key(user_id)) or 0)
    except Exception:
        return 0


def _take_attempt(user_id) -> int:
    """Count this attempt BEFORE the password is checked, and answer its number.

    Read first and counted after the check, the limit let every request already in
    flight through: twenty sent at once all read 0, all checked a guess, and twenty
    guesses passed a limit of five (the batch A review, 2026-11-23). The add-or-increment
    is one atomic step in the cache, so the sixth request at once is the sixth. 0 when
    the cache is down: fail open, as the platform's limiter does."""
    from training_platform.cache import ratelimit_cache

    cache = ratelimit_cache()
    key = _wrong_key(user_id)
    try:
        if cache.add(key, 1, WRONG_CURRENT_WINDOW_SECONDS):
            return 1
        try:
            return int(cache.incr(key))
        except ValueError:
            # The window ended between the two calls: this attempt opens the next one.
            cache.set(key, 1, WRONG_CURRENT_WINDOW_SECONDS)
            return 1
    except Exception:
        logger.warning("could not count a password change attempt for user %s", user_id,
                       exc_info=True)
        return 0


def _give_back(user_id) -> None:
    """The current password was right: this attempt was no guess, so it does not count."""
    from training_platform.cache import ratelimit_cache

    try:
        ratelimit_cache().decr(_wrong_key(user_id))
    except Exception:
        pass


def _throttled():
    response = Response(
        {'error': _('Too many attempts. Try again later.'), 'code': 'throttled',
         'retry_after_seconds': WRONG_CURRENT_WINDOW_SECONDS},
        status=status.HTTP_429_TOO_MANY_REQUESTS)
    response['Retry-After'] = str(WRONG_CURRENT_WINDOW_SECONDS)
    return response


class PasswordChangeView(APIView):
    """POST /api/auth/password/change/ — a new password for the signed-in person, and
    every other device signed out.

    Auth: Bearer JWT, any role. Body (JSON): `current_password`, `new_password`. Both
    are trimmed of surrounding spaces, as sign-in trims the password it is given.

    200 `{message, access, refresh}`: the password is changed, EVERY refresh token the
    account held is blacklisted (each other device is signed out when its access token
    expires, within the hour — `ACCESS_TOKEN_LIFETIME`, 60 minutes — an open chat socket
    included, at its first event past that), and this device gets a fresh pair — store both, the old
    refresh token no longer works. Failed sign-in counts are cleared.

    | status | `code` | when |
    |---|---|---|
    | `400` | `validation_error` | a field missing, or `new_password` refused by the sign-up rules: its own code in `field_errors.new_password[0].code` (`password_too_short`, `password_too_common`, `password_entirely_numeric`, `password_too_similar`) |
    | `400` | `current_password_wrong` | `current_password` is not the account's password, with `field`; nothing changes |
    | `400` | `password_unchanged` | `new_password` is the current one, with `field` |
    | `401` | `not_authenticated`, `token_not_valid` | no token; an expired or invalid one |
    | `429` | `throttled` | five wrong current passwords within 15 minutes — counted as each attempt arrives, so requests sent at once past the fifth are refused before their password is checked; `retry_after_seconds` and `Retry-After` say when to try again |

    The same on every build.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if _wrong_count(user.pk) >= MAX_WRONG_CURRENT:
            return _throttled()

        serializer = PasswordChangeSerializer(data=request.data, context={'user': user})
        serializer.is_valid(raise_exception=True)
        current = serializer.validated_data['current_password']
        new = serializer.validated_data['new_password']

        # The slot is taken before the guess is checked, never after (see _take_attempt).
        if _take_attempt(user.pk) > MAX_WRONG_CURRENT:
            return _throttled()
        if not user.check_password(current):
            return Response({'error': _('Your current password is not correct.'),
                             'code': 'current_password_wrong',
                             'field': 'current_password'},
                            status=status.HTTP_400_BAD_REQUEST)
        _give_back(user.pk)
        if user.check_password(new):
            return Response({'error': _('Choose a password different from your '
                                        'current one.'),
                             'code': 'password_unchanged', 'field': 'new_password'},
                            status=status.HTTP_400_BAD_REQUEST)

        from rest_framework_simplejwt.token_blacklist.models import (BlacklistedToken,
                                                                     OutstandingToken)
        from rest_framework_simplejwt.tokens import RefreshToken

        from .utils import clear_login_attempts

        with transaction.atomic():
            user.set_password(new)
            user.save(update_fields=['password'])
            # Every session ends: the same revocation the reset makes.
            for token in OutstandingToken.objects.filter(user=user):
                BlacklistedToken.objects.get_or_create(token=token)
        clear_login_attempts(user.email)
        fresh = RefreshToken.for_user(user)
        logger.info("Password changed for user %s; other sessions ended", user.pk)
        return Response({'message': _('Your password has been changed. Other devices '
                                      'have been signed out.'),
                         'access': str(fresh.access_token), 'refresh': str(fresh)},
                        status=status.HTTP_200_OK)
