"""The base row for each channel, as the payments and store-compliance spec sets them.

Written through the historical model, so no change-log rows: the log starts with the
first change an operator makes. Re-running is harmless — a channel that already has a
base row is left exactly as it is.
"""
from django.db import migrations

BASE_ROWS = {
    "apk": dict(can_purchase_platform_sub=True, can_purchase_routine_product=True,
                can_pay_coaching_in_app=True, can_show_prices=True,
                can_show_coaching_prices=True, can_show_web_hub_link=True,
                can_view_entitlements=True, can_view_wallet=True, can_transfer_wallet=True),
    "play": dict(can_purchase_platform_sub=False, can_purchase_routine_product=False,
                 can_pay_coaching_in_app=True, can_show_prices=False,
                 can_show_coaching_prices=True, can_show_web_hub_link=False,
                 can_view_entitlements=True, can_view_wallet=True, can_transfer_wallet=True),
    # First submission: no purchasing of any kind. Coaching is enabled in a later,
    # reviewed update by adding a row with a higher `min_version`.
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


def seed(apps, schema_editor):
    ChannelCapabilities = apps.get_model("policy", "ChannelCapabilities")
    for channel, flags in BASE_ROWS.items():
        ChannelCapabilities.objects.get_or_create(
            channel=channel, min_version="0.0.0", defaults=flags)


class Migration(migrations.Migration):

    dependencies = [
        ("policy", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
