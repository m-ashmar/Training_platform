"""SIDE_BUGS S-162 (PAY-3, the ruled numbers): the "large top-up" alert's thresholds are
set — 1,000,000 SYP and 100 USD — and readable from the environment.

The mechanism (one threshold per currency, cash top-ups only) landed in 08f5f27; only
the numbers were owed, and with none set both currencies fell back to the old 1,000,
which flagged ordinary pound top-ups and let a dollar top-up worth millions of pounds
through. `WALLET_LARGE_TOPUP_THRESHOLDS` now carries them in `settings_base`, each from
`WALLET_LARGE_TOPUP_THRESHOLD_SYP` / `_USD`, with a line in `deploy/env.example`.
"""
import pathlib
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_ruled_numbers_are_the_defaults():
    from django.conf import settings

    from wallet.views import large_topup_thresholds

    assert settings.WALLET_LARGE_TOPUP_THRESHOLDS == {"SYP": Decimal("1000000"),
                                                      "USD": Decimal("100")}
    assert large_topup_thresholds() == {"SYP": Decimal("1000000"), "USD": Decimal("100")}


def test_each_is_read_from_the_environment_and_named_in_the_deploy_template():
    source = (ROOT / "training_platform" / "settings_base.py").read_text()
    template = (ROOT / "deploy" / "env.example").read_text()
    checklist = (ROOT / "DEPLOY_CHECKLIST.md").read_text()
    for name in ("WALLET_LARGE_TOPUP_THRESHOLD_SYP", "WALLET_LARGE_TOPUP_THRESHOLD_USD"):
        assert f'get_env("{name}"' in source, name
        assert f"#{name}=" in template, name
        assert name in checklist, name


@pytest.mark.parametrize("currency, below, at", [("SYP", "999999.00", "1000000.00"),
                                                 ("USD", "99.00", "100.00")])
def test_the_alert_flags_a_cash_top_up_at_the_threshold(make_user, api, currency, below, at):
    from wallet.models import move_funds_atomic, set_currency_for, wallet_for

    flagged = []
    for tag, amount in (("below", below), ("at", at)):
        agent = make_user(f"s162_{currency}_{tag}_agent", user_type="agent")
        client = make_user(f"s162_{currency}_{tag}")
        set_currency_for(client, currency, "client")
        move_funds_atomic(None, wallet_for(client, "client"), Decimal(amount),
                          actor_id=agent.pk, tx_type="topup", currency=currency,
                          metadata={"agent_id": agent.pk})
        flagged.append(agent.pk)
    admin = make_user(f"s162_{currency}_admin", user_type="admin")
    alerts = api(admin).get("/api/wallet/admin/alerts/suspicious/").json()["alerts"]
    large = [(a["actor_id"], a.get("currency")) for a in alerts if a["type"] == "large_topups"]
    assert large == [(flagged[1], currency)], large
