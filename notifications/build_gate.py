"""Which text a BUILD is shown for a notification (Road to 10 R1, 2026-11-24).

A notification is written for an account, not for a build: the same inbox row is read,
and the same push is sent, on every device the person has. A few sentences invite a
payment that a store build cannot make (the app's store-safety test; the manager's
rulings of 2026-11-24 under the owner's delegation). Those events declare the policy
flag they need, and this module picks, for one build:

* the event's own `template`, when the build has the flag (every apk and web build: they
  have every flag, so for them nothing changes);
* its `store_template`, when it has one and the build lacks the flag;
* nothing at all (`HIDDEN`), when it has none: the inbox leaves the row out of the list
  and of the unread count, and no push goes to that device.

Rule zero: the decision reads the BUILD and nothing else — `policy.services.flags_for`
of the channel and version the request declares (`X-App-Channel`, `X-App-Version`), or
that a device token recorded when it was registered. Never the user, a country, a date
or a review mode. A request that declares no channel, and a token whose build was never
recorded, get `policy.services.STRICTEST`, as on every route — except a token the app
registered as `platform: ios`, which can only be the App Store build.

Today three events are gated (each class says so beside its template):

  checkout_expired      needs can_purchase_platform_sub   no store text: hidden
  coaching_payment_due  needs can_pay_coaching_in_app     a store text
  subscription_expired  needs can_purchase_platform_sub   a store text

Records of money that already moved (a receipt, a fee held or paid out, a top-up, a sale,
a withdrawal) and the state of an entitlement declare nothing and read the same on every
build.
"""
from __future__ import annotations

import importlib

class _Hidden:
    """What `choose` answers for a build that may not see the event at all. Not None:
    None is what an event with no template answers, which the inbox and the push already
    handle (the stored title and body, or the safe fallback sentence)."""

    def __repr__(self):
        return 'HIDDEN'


HIDDEN = _Hidden()


def event_class(event_type: str):
    """The domain event class registered for `event_type`, or None."""
    from notifications.channels.fcm import EVENT_CLASS_REGISTRY

    path = EVENT_CLASS_REGISTRY.get(event_type)
    if not path:
        return None
    module_path, name = path.rsplit('.', 1)
    try:
        return getattr(importlib.import_module(module_path), name)
    except (ImportError, AttributeError):
        return None


def choose(event_type: str, flags: dict):
    """The template this build is shown for `event_type`, or `HIDDEN`.

    An event that declares no `requires` — every record and every other notice — always
    answers its own template, so nothing changes for it on any build. An event type with
    no registered class answers None, as `channels.fcm._resolve_event_template` does."""
    cls = event_class(event_type)
    if cls is None:
        return None
    template = getattr(cls, 'template', None)
    needs = getattr(cls, 'requires', None)
    if not needs or flags.get(needs, False):
        return template
    return getattr(cls, 'store_template', None) or HIDDEN


def gated_event_types() -> dict:
    """{event_type: the flag it needs}, for every event that declares one."""
    from notifications.channels.fcm import EVENT_CLASS_REGISTRY

    gated = {}
    for event_type in EVENT_CLASS_REGISTRY:
        cls = event_class(event_type)
        needs = getattr(cls, 'requires', None) if cls is not None else None
        if needs:
            gated[event_type] = needs
    return gated


def hidden_event_types(flags: dict) -> list:
    """The event types this build is not shown at all: a gated event with no store text
    whose flag the build lacks. The inbox leaves them out of the list and the count."""
    hidden = []
    for event_type, needs in gated_event_types().items():
        if not flags.get(needs, False) and not getattr(event_class(event_type),
                                                       'store_template', None):
            hidden.append(event_type)
    return sorted(hidden)


def flags_for_request(request) -> dict:
    """The reading build's flags: policy's own, from the two headers alone."""
    from policy.services import flags_for_request as policy_flags

    return policy_flags(request)


def flags_for_token(app_channel, app_version, platform) -> dict:
    """The flags of the build that registered a device token.

    The channel it declared when it registered (`DeviceToken.app_channel`). Unknown —
    a token registered before the build was recorded, or by a request that declared
    none — is the strictest build, as policy answers a missing channel; a token
    registered as `platform: ios` is the App Store build (the manager's ruling,
    2026-11-24: the only iOS build there is)."""
    from policy.channels import Channel, parse_version
    from policy.services import flags_for

    version = parse_version(app_version)
    if app_channel in Channel.values:
        return flags_for(app_channel, version)
    if platform == 'ios':
        return flags_for(Channel.IOS, version)
    return flags_for(None)
