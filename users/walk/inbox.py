"""Walk states: inbox (`users/walk/__init__.py` says how to add one).

Owned by the phase 3 worker. Examples of what belongs here: an inbox holding one notice of every kind, for a client and for a trainer.
Every state goes through the platform's real services, never a hand-written ledger row,
payment state or hold.
"""
from . import state  # noqa: F401  (the decorator states in this module register with)
