"""Personal-data lifecycle.

Before this existed, the only privacy capability in the project was
`DELETE /api/ai/data/` — one endpoint covering one app. Export, erasure and retention
were each about to be built per-app, which is how they drift apart: an app added later
is simply forgotten by all three, silently.

Instead each app registers what personal data it holds, once, and the three operations
are derived from that single registry:

    export(user)    -> everything held about them          (GDPR Art. 15)
    erase(user)     -> anonymise/remove, ledger preserved  (GDPR Art. 17)
    purge_expired() -> retention, applied uniformly        (storage limitation)

Adding a model to the registry gives all three at once. Forgetting to register a model
is visible — `audit_coverage()` lists every model holding a user FK that nobody claimed.
"""
from . import sources  # noqa: F401  (registers every app's data on import)
from .registry import (  # noqa: F401
    PersonalDataSource,
    audit_coverage,
    erase_user_data,
    export_user_data,
    purge_expired,
    register,
    registry,
    validate_sources,
)
