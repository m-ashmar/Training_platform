"""Walk states: the operator's pages (`users/walk/__init__.py` says how to add one).

Owned by the phase 3 worker.
"""
from . import state


@state("operator_staff", area="operator", role="admin",
       summary="An operator for the staff page: an active admin, so the console and the "
               "operator's API (queue counts, console sign-in) answer them.")
def operator_staff(walk):
    me = walk.account(first_name="مروى", last_name="المشغّلة")
    # `CustomUser.save()` makes every admin staff: the console's door
    # (`admin_dashboard/operators.py is_console_operator`).
    assert me.is_staff and me.is_admin
    walk.ids()
