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
