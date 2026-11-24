"""Walk states: wallet and agents (`users/walk/__init__.py` says how to add one).

Owned by the money worker. Examples of what belongs here: a wallet topped up by an agent, an agent holding the platform's cash, a withdrawal.
Every state goes through the platform's real services, never a hand-written ledger row,
payment state or hold.
"""
from . import state  # noqa: F401  (the decorator states in this module register with)
