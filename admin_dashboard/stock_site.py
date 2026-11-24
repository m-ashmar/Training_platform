"""The stock Django admin at `/admin/`, behind the console's door (the owner, 2026-11-24).

`/admin/` is `django.contrib.admin`'s own site. It registers about thirty models the
console at `/dj-admin/` does not — the wallets, the ledger, the agents' API keys among
them — and let in any active STAFF account, Django's default: `is_active and is_staff`.
This project has staff accounts that are not operators (`admin_dashboard/operators.py`),
so such an account could read and act on the platform's money there although no console
queue would answer it.

Now only an account the console counts as an operator gets in: `is_console_operator`, a
superuser or an `admin` account, active and staff.

* `OperatorsOnlyAdminSite.has_permission` — every page of the site asks it, so an
  operator demoted while signed in is turned away at the next page.
* `OperatorLoginForm` — a staff account that is not an operator is refused at the login
  form itself, with the same message as a wrong password (the form does not say the
  account exists), and no session is made for it.

The models registered there, and the permissions their actions declare, are unchanged.
`admin_dashboard/stock_admin_config.py OperatorsOnlyAdminConfig` makes this the default
site: `INSTALLED_APPS` names it in place of `django.contrib.admin` (Django's documented
way to replace `admin.site`), so every `admin.site.register` and `@admin.register` in the
project lands on it unchanged.
"""
from django.contrib.admin import AdminSite
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.exceptions import ValidationError

from .operators import is_console_operator


class OperatorLoginForm(AdminAuthenticationForm):
    """The stock admin's login form, refusing a staff account that is not an operator
    with the wrong-password message."""

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not is_console_operator(user):
            raise ValidationError(self.error_messages["invalid_login"],
                                  code="invalid_login",
                                  params={"username": self.username_field.verbose_name})


class OperatorsOnlyAdminSite(AdminSite):
    """`django.contrib.admin`'s site, for console operators only."""

    login_form = OperatorLoginForm

    def has_permission(self, request):
        return is_console_operator(getattr(request, "user", None))
