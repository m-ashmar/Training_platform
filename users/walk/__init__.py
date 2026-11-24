"""The walk-through states: ordinary accounts put into the states the app's walk-throughs
need (`manage.py seed_walk_states`, development only).

**What a state is.** One function, registered with `@state`, that is handed a `Walk`
(`users/walk/framework.py`) and makes its accounts and rows through the platform's REAL
service functions — the coaching request and `approve_trainer_relation`, `users/coaching.py`,
`PaymentService`, the agent top-up path with its `money_type`, `assign_routine`, the
schedule — so every state is one the server can really reach. A state that time makes
moves the dates and then calls the function the scheduled task calls. A state never
writes a ledger row, a payment state or a hold by hand.

**Where states live: one module per area**, so several people add states without editing
the same file:

    users/walk/coaching.py   coaching and plans
    users/walk/training.py   programmes and training
    users/walk/trainer.py    trainer and products
    users/walk/wallet.py     wallet and agents
    users/walk/community.py  community
    users/walk/inbox.py      inbox
    users/walk/operator.py   the operator's pages
    users/walk/progress.py   progress, badges and body measurements

`users/walk/shared.py` holds what several areas use (a coaching made through the request
and its answer). `walk.api(user, method, path, data)` calls a real route as an account,
with the build declared, for what has no service function.

**Adding one.** In the area's module::

    from . import state

    @state("client_waiting_for_coach", area="coaching", role="client",
           summary="A client whose request a trainer has not answered yet.")
    def client_waiting_for_coach(walk):
        me = walk.account()                                 # walk.<state>@yalla.test
        coach = walk.account("trainer", role="trainer")     # walk.<state>.trainer@yalla.test
        ...                                                 # the real services
        walk.ids(trainer=coach.pk, request=relation.pk)     # what the scripts read

The name is the state's id in the manifest and in its addresses: lower case, digits and
`_` only. Every account a state makes is a `walk.<state>…@yalla.test` account, and those
are the only accounts the command ever touches.

**Nothing reads these accounts but the command.** Rule zero: the server serves a walk
account exactly what it serves any account. There is no flag, group or column that marks
one, and nothing at request time may look at the address.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Callable

#: The areas, each a module of this package.
AREAS = {
    "coaching": "coaching and plans",
    "training": "programmes and training",
    "trainer": "trainer and products",
    "wallet": "wallet and agents",
    "community": "community",
    "inbox": "inbox",
    "operator": "the operator's pages",
    "progress": "progress, badges and body measurements",
}

ROLES = ("client", "trainer", "agent", "admin")

#: A state's name: it becomes part of an address (`walk.<name>@yalla.test`).
NAME = re.compile(r"^[a-z][a-z0-9_]{1,60}$")

#: Names a state may not take: `retired` is where a reset moves old accounts.
RESERVED = frozenset({"retired"})


@dataclass(frozen=True)
class State:
    name: str
    area: str
    role: str
    summary: str
    build: Callable


#: Every registered state, by name.
STATES: dict[str, State] = {}


def state(name: str, *, area: str, role: str, summary: str):
    """Register a state. `role` is the role of its main account (`walk.account()`)."""
    if not NAME.match(name) or name in RESERVED:
        raise ValueError(f"walk state name {name!r}: lower case, digits and _ only, "
                         f"and not {sorted(RESERVED)}")
    if area not in AREAS:
        raise ValueError(f"walk state {name!r}: area {area!r} is not one of {sorted(AREAS)}")
    if role not in ROLES:
        raise ValueError(f"walk state {name!r}: role {role!r} is not one of {ROLES}")

    def register(build):
        if name in STATES and STATES[name].build is not build:
            raise ValueError(f"walk state {name!r} is registered twice")
        STATES[name] = State(name=name, area=area, role=role, summary=summary, build=build)
        return build

    return register


def load_states() -> dict[str, State]:
    """Import every area module, so each registers its states, and answer the registry."""
    for area in AREAS:
        importlib.import_module(f"{__name__}.{area}")
    return STATES
