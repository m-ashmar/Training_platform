"""Who receives each notification kind, by role (Road to 10 S0.4, 2026-11-22).

`GET /api/notifications/preferences/event_types/` listed every kind to every account, so
a trainer's settings page offered «تذكير الجلسة» and the plan's renewal notices, which
no trainer is ever sent, and the app could not tell which rows to draw. This is the
answer, read from where each kind is emitted (the call site's recipient), so the page
shows the kinds this account's role can receive and nothing else. It decides nothing
about delivery: a preference for any listed kind is still accepted and stored, and the
inbox receives everything regardless (BACKEND_BUGS B-014).

A kind sent to more than one role lists each. A new kind in
`notifications/channels/fcm.py EVENT_CLASS_REGISTRY` must be added here
(`tests/test_road10_s04_trainer_account.py` fails until it is).
"""
from __future__ import annotations

CLIENT, TRAINER, AGENT, ADMIN = 'client', 'trainer', 'agent', 'admin'
MEMBERS = frozenset({CLIENT, TRAINER})

#: kind -> the roles a call site sends it to.
AUDIENCE = {
    # Social: anyone with a profile can be liked, followed, commented on, awarded or in a
    # challenge (the owner's D11: any member may host one).
    'post_liked': MEMBERS,
    'comment_created': MEMBERS,
    'user_followed': MEMBERS,
    'achievement_awarded': MEMBERS,
    'challenge_progress': MEMBERS,
    # Requests and coaching (users/views.py, users/coaching.py).
    'trainer_assignment_request': frozenset({CLIENT}),
    'trainer_unassignment': frozenset({CLIENT}),
    'client_request_received': frozenset({TRAINER}),
    'client_request_approved': MEMBERS,
    'client_request_rejected': MEMBERS,
    'client_request_cancelled': frozenset({TRAINER}),
    'client_unassigned_trainer': frozenset({TRAINER}),
    'coaching_renewed': frozenset({CLIENT}),
    'coaching_renewal_soon': frozenset({CLIENT}),
    'coaching_ending_soon': frozenset({CLIENT}),
    'coaching_payment_due': frozenset({CLIENT}),
    'coaching_ended': MEMBERS,
    'coaching_finished': MEMBERS,
    'coaching_fee_held': MEMBERS,
    'coaching_fee_released': MEMBERS,
    'coaching_fee_refunded': MEMBERS,
    # The platform plan and its payments: the payer (subscription/, payment_service).
    'subscription_expiring': frozenset({CLIENT}),
    'subscription_expired': frozenset({CLIENT}),
    'checkout_expired': frozenset({CLIENT}),
    'payment_needs_review': frozenset({CLIENT}),
    'payment_rejected': frozenset({CLIENT}),
    'payment_confirmed': frozenset({CLIENT}),
    # The trainer's own business.
    'trainer_verification_approved': frozenset({TRAINER}),
    'trainer_verification_rejected': frozenset({TRAINER}),
    'withdrawal_requested': frozenset({TRAINER}),
    'withdrawal_paid': frozenset({TRAINER}),
    'withdrawal_failed': frozenset({TRAINER}),
    'routine_product_approved': frozenset({TRAINER}),
    'routine_product_rejected': frozenset({TRAINER}),
    'routine_product_sold': frozenset({TRAINER}),
    'routine_product_sale_refunded': frozenset({TRAINER}),
    'trainer_weekly_digest': frozenset({TRAINER}),
    # The buyer's side of a programme, the wallet, the training.
    'routine_product_purchased': frozenset({CLIENT}),
    'routine_product_refunded': frozenset({CLIENT}),
    'wallet_topped_up': frozenset({CLIENT}),
    'personal_record': frozenset({CLIENT}),
    'weekly_recap': frozenset({CLIENT}),
    'routine_assignment': frozenset({CLIENT}),
    'session_reminder': frozenset({CLIENT}),
    'progress_milestone': frozenset({CLIENT}),
    'custom': frozenset({CLIENT}),
    # Both sides of a thread, both sides of a finished session.
    'chat_message': MEMBERS,
    'session_completed': MEMBERS,
}


def kinds_for(role, kinds) -> list[str]:
    """The kinds of `kinds` this role is sent, sorted; `all` (the master switch) is kept
    for every role. An unknown role keeps only `all`."""
    return sorted(kind for kind in kinds
                  if kind == 'all' or role in AUDIENCE.get(kind, frozenset()))
