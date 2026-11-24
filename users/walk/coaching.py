"""Walk states: coaching and plans (`users/walk/__init__.py` says how to add one).

Owned by the money worker. Examples of what belongs here: a request waiting, an approved month held, a paid month, a frozen coaching, a renewal due, a plan bought or ending.
Every state goes through the platform's real services, never a hand-written ledger row,
payment state or hold.
"""
from . import state  # noqa: F401  (the decorator states in this module register with)
