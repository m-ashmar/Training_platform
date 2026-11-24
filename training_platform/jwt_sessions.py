"""A password change signs every device out AT ONCE (the owner, 2026-11-24).

Before, `users/utils.py end_every_session` blacklisted the account's refresh tokens and
every other device kept its ACCESS token until it expired, up to an hour later
(`ACCESS_TOKEN_LIFETIME`), an open chat socket included.

THE MECHANISM: simplejwt's own revocation claim, `CHECK_REVOKE_TOKEN`
(`settings_base.SIMPLE_JWT`). Every token simplejwt mints (`Token.for_user`: sign-in,
the OTP verification, the password change's fresh pair, the tests' and the walk seed's
pairs) carries `hash_password`, a digest of the account's password hash as it was when
the token was made. A token whose digest is not the account's current one is refused.
Every way the hash changes is covered without any of them having to remember: the
password change, the forgot-password reset, the console's reset
(`set_unusable_password`), the console's password field.

Why this and not a counter of our own on the account: no migration, and no extra query
— the check reads `password` off the user row that authentication loads anyway (one
query, as before). A counter would have needed every place that mints a token to write
it, and a token made by a path that forgot would have been refused after the
account's first change.

ONE DEPARTURE from simplejwt, and the reason for this module: simplejwt refuses a token
that carries NO digest, and every token issued before this deploy carries none. Taken
as it comes, the deploy would have signed every account out once, and the refresh would
not have saved them — a refreshed access token copies the refresh token's claims, so it
would have lacked the digest too, for as long as the 7-day refresh chain lived. Here a
token without the digest is still accepted (`session_is_current`), and the first
refresh of such a token gives it the digest (`SessionTokenRefreshSerializer`). So on
the day of the deploy nobody signs in again. A token without the digest can only be one
issued before the deploy: an access token among them lives out its hour; a refresh
token among them is blacklisted by a password change like any other, and gains the
digest at its first use.

What ends at once from then on, on every route, including the cached ones
(`middleware.CacheMiddleware`): the access tokens of every device, and an open socket
(`training_platform.ws_auth`). A deactivated account was already refused at once
(`is_active`, read on the same row); its sockets now close at once too, through the
same `end_every_session`.

A side effect worth knowing: Django re-hashes a password when its hasher's work factor
rises (an upgrade of Django), at the account's next password sign-in. That changes the
hash, so that sign-in signs the account's OTHER devices out, once.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.utils import get_md5_hash_password

#: The 401 `code` of a token made before the account's password last changed.
#: Additive; the app refreshes on any 401, and the refresh token was blacklisted by the
#: same change, so the person signs in again.
PASSWORD_CHANGED = "password_changed"


def password_digest(password_hash: str) -> str:
    """The digest a token carries for this password hash (simplejwt's own)."""
    return get_md5_hash_password(password_hash or "")


def digest_is_current(token, digest: str) -> bool:
    """Does `token` carry `digest`, the account's current one? A token without any
    was issued before the deploy of 2026-11-24 and is accepted (module docstring)."""
    claim = token.get(api_settings.REVOKE_TOKEN_CLAIM)
    return claim is None or claim == digest


def session_is_current(token, password_hash: str) -> bool:
    """Was `token` made under the account's CURRENT password (`password_hash`)?"""
    return digest_is_current(token, password_digest(password_hash))


def _password_changed():
    return AuthenticationFailed(_("Your password was changed. Sign in again."),
                                code=PASSWORD_CHANGED)


class SessionJWTAuthentication(JWTAuthentication):
    """`JWTAuthentication`, with the revocation claim checked as `session_is_current`
    says (simplejwt's own check refuses every token issued before the deploy). The same
    single query for the user as the parent; the rest is the parent's, line for line."""

    def get_user(self, validated_token):
        try:
            user_id = validated_token[api_settings.USER_ID_CLAIM]
        except KeyError as exc:
            raise InvalidToken(_("Token contained no recognizable user identification")) from exc
        try:
            user = self.user_model.objects.get(**{api_settings.USER_ID_FIELD: user_id})
        except self.user_model.DoesNotExist as exc:
            raise AuthenticationFailed(_("User not found"), code="user_not_found") from exc
        if api_settings.CHECK_USER_IS_ACTIVE and not user.is_active:
            raise AuthenticationFailed(_("User is inactive"), code="user_inactive")
        if not session_is_current(validated_token, user.password):
            raise _password_changed()
        return user


class SessionTokenRefreshSerializer(TokenRefreshSerializer):
    """`POST /api/auth/token/refresh/`: simplejwt's refresh, with the revocation claim.

    A refresh token made under an older password is refused, 401 `password_changed`
    (the change blacklists it too; this holds even if the blacklisting had failed). A
    refresh token from before the deploy, without the digest, is given it, so the
    access token it issues and the refresh token it rotates into carry it.

    Otherwise simplejwt's `TokenRefreshSerializer.validate`, step for step, and the
    same queries: one for the user, the blacklist's own. One difference: a token whose
    account no longer exists answers 401 `no_active_account` where the parent raised
    `DoesNotExist`, a 500."""

    def validate(self, attrs):
        refresh = self.token_class(attrs["refresh"])

        user_id = refresh.payload.get(api_settings.USER_ID_CLAIM, None)
        user = (get_user_model().objects.filter(**{api_settings.USER_ID_FIELD: user_id})
                .first() if user_id else None)
        if user is None or not api_settings.USER_AUTHENTICATION_RULE(user):
            raise AuthenticationFailed(self.error_messages["no_active_account"],
                                       "no_active_account")
        if not session_is_current(refresh, user.password):
            raise _password_changed()
        if refresh.payload.get(api_settings.REVOKE_TOKEN_CLAIM) is None:
            refresh[api_settings.REVOKE_TOKEN_CLAIM] = password_digest(user.password)

        data = {"access": str(refresh.access_token)}

        if api_settings.ROTATE_REFRESH_TOKENS:
            if api_settings.BLACKLIST_AFTER_ROTATION:
                try:
                    refresh.blacklist()
                except AttributeError:
                    pass
            refresh.set_jti()
            refresh.set_exp()
            refresh.set_iat()
            refresh.outstand()
            data["refresh"] = str(refresh)

        return data
