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

    def get_client_ip(self, request):
        """The trusted client address, as the rate limiter decides it.

        allauth's default takes the LEFT-most X-Forwarded-For entry, which is whatever
        the client wrote there, so a per-IP allauth rate limit keyed on it could be
        rotated at will. One helper decides the client address for the whole app.
        """
        from training_platform.middleware import get_trusted_client_ip

        return get_trusted_client_ip(request)
