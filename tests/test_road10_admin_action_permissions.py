"""Every console action declares the permission it needs (Road to 10 round 4, from the
batch A review, 2026-11-24).

Django offers an action with no `allowed_permissions` to anyone who can SEE the
changelist: a staff account given only `view_customuser` deactivated accounts and took
pictures down, and one with `view_subscription` granted paid access ("Activate selected
subscriptions"). Each action now names what it needs (`permissions=[...]`, answered by the
admin's own `has_<perm>_permission`). The sweep walks every ModelAdmin of BOTH admin
sites (the console `/dj-admin/` and the stock `/admin/`) so the next action cannot forget;
read-only exports are allowed by name, each with its reason.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

#: Actions allowed with no declared permission: read-only, each with why. Anything that
#: writes, sends or grants is NOT allowed here.
READ_ONLY = {
    ("diet.FoodItem", "export_as_csv"):
        "downloads the selected rows as CSV and changes nothing; it needs exactly what "
        "the changelist already needs (view)",
}


def _actions_without_permission():
    from django.contrib.admin.sites import all_sites

    missing, unanswered = [], []
    for site in all_sites:
        for model, model_admin in site._registry.items():
            for func, name, _description in model_admin._get_base_actions():
                key = (model._meta.label, name)
                perms = getattr(func, "allowed_permissions", None)
                if not perms:
                    if key not in READ_ONLY:
                        missing.append(f"{site.name}: {model._meta.label}.{name}")
                    continue
                for perm in perms:
                    if not callable(getattr(model_admin, f"has_{perm}_permission", None)):
                        unanswered.append(f"{site.name}: {model._meta.label}.{name} "
                                          f"needs has_{perm}_permission")
    return sorted(missing), sorted(unanswered)


def test_every_action_of_every_admin_declares_its_permission():
    missing, unanswered = _actions_without_permission()
    assert not missing, "actions any viewer can run:\n" + "\n".join(missing)
    assert not unanswered, "\n".join(unanswered)


def test_the_read_only_allowance_names_only_actions_that_exist():
    """A stale entry would let a new action by the same name through."""
    from django.contrib.admin.sites import all_sites

    present = {(model._meta.label, name)
               for site in all_sites for model, model_admin in site._registry.items()
               for _func, name, _d in model_admin._get_base_actions()}
    assert set(READ_ONLY) <= present


def _viewer(make_user, codename):
    from django.contrib.auth.models import Permission

    viewer = make_user(f"aap_view_{uuid.uuid4().hex[:6]}", is_staff=True)
    viewer.user_permissions.add(Permission.objects.get(codename=codename))
    return viewer


def test_a_view_only_staff_account_cannot_deactivate_accounts(make_user, client):
    target = make_user(f"aap_target_{uuid.uuid4().hex[:6]}")
    client.force_login(_viewer(make_user, "view_customuser"))
    client.post("/dj-admin/users/customuser/",
                {"action": "deactivate_users", "_selected_action": [target.pk]})
    target.refresh_from_db()
    assert target.is_active, "a view-only account deactivated another account"


def test_a_view_only_staff_account_cannot_grant_a_subscription(make_user, client):
    from subscription.models import Subscription
    from tests.test_fx_subs import _plan

    person = make_user(f"aap_sub_{uuid.uuid4().hex[:6]}")
    sub = Subscription.objects.create(user=person, plan=_plan("aap plan", "13000.00"),
                                      status="pending")
    client.force_login(_viewer(make_user, "view_subscription"))
    client.post("/dj-admin/subscription/subscription/",
                {"action": "activate_subscriptions", "_selected_action": [sub.pk]})
    sub.refresh_from_db()
    assert sub.status == "pending", "a view-only account granted paid access"


def test_an_operator_still_runs_them(make_user, client):
    """The change permission is what an operator holds: the actions stay theirs."""
    target = make_user(f"aap_op_target_{uuid.uuid4().hex[:6]}")
    client.force_login(make_user(f"aap_operator_{uuid.uuid4().hex[:6]}", user_type="admin"))
    client.post("/dj-admin/users/customuser/",
                {"action": "deactivate_users", "_selected_action": [target.pk]})
    target.refresh_from_db()
    assert target.is_active is False
