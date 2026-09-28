"""
users/adapters.py - Custom allauth account adapter.

Disables the native allauth/dj-rest-auth signup flow entirely.
All registration MUST go through the custom OTP-gated CustomRegisterView.
This prevents auth bypass via dj-rest-auth's un-gated registration endpoint.
"""

from allauth.account.adapter import DefaultAccountAdapter


class CustomAccountAdapter(DefaultAccountAdapter):
    """Block all signups via allauth/dj-rest-auth. Force OTP-gated flow."""

    def is_open_for_signup(self, request):
        return False

    def generate_unique_username(self, txts, regex=None):
        """The name a sign-up with no `username` gets, from its e-mail's local part.

        That name is public (posts, boards) and cannot be changed later, but nobody typed
        it, so it is never refused: when it holds a term on the banned-terms list, or is
        one of the platform's reserved ledger names (`platform_escrow`, ...), the account
        is named `user`, `user1`, ... instead (the owner, 2026-09-28; BACKEND_BUGS B-172).
        A `username` the request DOES send is checked by the register serializer.
        """
        from moderation.ugc_filter import find_banned
        from wallet.utils import is_reserved_username

        name = super().generate_unique_username(txts, regex)
        if find_banned(name) or is_reserved_username(name):
            return super().generate_unique_username(["user"], regex)
        return name

    def get_client_ip(self, request):
        """The trusted client address, as the rate limiter decides it.

        allauth's default takes the LEFT-most X-Forwarded-For entry, which is whatever
        the client wrote there, so a per-IP allauth rate limit keyed on it could be
        rotated at will. One helper decides the client address for the whole app.
        """
        from training_platform.middleware import get_trusted_client_ip

        return get_trusted_client_ip(request)
