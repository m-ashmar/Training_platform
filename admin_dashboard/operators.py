"""Who is a console operator, said once for the API as the console says it.

The console at `/dj-admin/` lets a person in on two conditions, and each queue page
adds a third:

* `AdminSite.has_permission`: `is_active and is_staff` — the door of the console;
* every queue the operator's phone counts (`moderation/admin.py`,
  `trainer_verification/admin.py`, `payouts/admin.py`): an ACTUAL admin —
  `is_superuser` or `user_type == 'admin'` — "not merely a staff account", because
  this project has staff accounts that are not operators.

An API that shows what those pages hold, or opens them, must not answer anyone the
pages themselves would turn away, so it asks all three. `CustomUser.save()` makes every
`admin` account staff, so in practice this is "an active admin, or a superuser who is
staff".
"""
from django.utils.translation import gettext_lazy as _
from rest_framework.permissions import BasePermission


def is_console_operator(user) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False)
                and user.is_active and user.is_staff
                and (user.is_superuser or getattr(user, "is_admin", False)))


class ConsoleOperatorsOnly(BasePermission):
    """Anyone the console's queues would turn away: 403 `admins_only` — the code the
    wallet's and the subscription app's operator endpoints answer for the same fact."""
    code = "admins_only"
    message = _("Only an administrator can do this.")

    def has_permission(self, request, view):
        return is_console_operator(getattr(request, "user", None))
