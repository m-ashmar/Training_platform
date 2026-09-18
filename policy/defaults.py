"""What each channel may do when no row says otherwise — the spec's own table.

Rows in `ChannelCapabilities` override this; they do not have to exist for the platform
to work. With the table as the only source, losing the rows (a flush, a restore that
missed them, a database whose data migrations were faked) refused every purchase on
every channel, the direct APK included. A store build gains nothing from this default:
what it may never do is locked in `policy.models.STORE_LOCKED_FLAGS` either way.

`policy/migrations/0002` seeds the same values so the operator console has rows to edit;
`tests/test_policy_channels.py` fails if the two ever differ.
"""

DEFAULT_FLAGS = {
    "apk": dict(can_purchase_platform_sub=True, can_purchase_routine_product=True,
                can_pay_coaching_in_app=True, can_show_prices=True,
                can_show_coaching_prices=True, can_show_web_hub_link=True,
                can_view_entitlements=True, can_view_wallet=True, can_transfer_wallet=True),
    "play": dict(can_purchase_platform_sub=False, can_purchase_routine_product=False,
                 can_pay_coaching_in_app=True, can_show_prices=False,
                 can_show_coaching_prices=True, can_show_web_hub_link=False,
                 can_view_entitlements=True, can_view_wallet=True, can_transfer_wallet=True),
    # First submission: no purchasing of any kind. Coaching is enabled in a later,
    # separately reviewed build by adding a row with a higher `min_version`.
    "ios": dict(can_purchase_platform_sub=False, can_purchase_routine_product=False,
                can_pay_coaching_in_app=False, can_show_prices=False,
                can_show_coaching_prices=False, can_show_web_hub_link=False,
                can_view_entitlements=True, can_view_wallet=True, can_transfer_wallet=False),
    # The hub IS the web channel, so a link to itself means nothing there.
    "web": dict(can_purchase_platform_sub=True, can_purchase_routine_product=True,
                can_pay_coaching_in_app=True, can_show_prices=True,
                can_show_coaching_prices=True, can_show_web_hub_link=False,
                can_view_entitlements=True, can_view_wallet=True, can_transfer_wallet=True),
}
