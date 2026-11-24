"""Walk states: trainer and products (`users/walk/__init__.py` says how to add one).

Owned by the batch B worker. Examples of what belongs here: a trainer with clients and a queue, a product listed or in review, a sale.
Every state goes through the platform's real services, never a hand-written ledger row,
payment state or hold.
"""
from . import state  # noqa: F401  (the decorator states in this module register with)
