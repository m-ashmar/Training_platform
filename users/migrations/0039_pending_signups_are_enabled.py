"""A signup that has not verified yet is enabled and unverified, not disabled.

0038 split "email verified" out of "account enabled", but registration kept writing
`is_active=False` to mean "not verified yet", so a pending signup and a suspended account
both read `is_active=False` and resend-otp refused both. Measured: register 201, the
10-minute code expires, resend answers 200 and sends nothing, and the address can never
be verified, logged into, reset or registered again. Registration now writes
`is_active=True, email_verified=False`; this moves the signups already stranded in the
old shape into the new one, so they can ask for a code.

Only rows that registration can have written, and nothing else that is inactive and
unverified:

- never signed in (`last_login` is NULL — the token and login endpoints both stamp it);
- a usable password (the escrow ledger owner has none);
- not staff, not a superuser;
- not an erased account (erasure rewrites the address to `@invalid.local`).

An account disabled while still unverified, or disabled before 0038 without ever having
signed in through login or token, cannot be told apart from a pending signup in the old
shape — that ambiguity is the defect. It lands as a pending signup: it may request a
code, and has to prove its address before it gets a credential. The product is not in
production, so this is a statement about test and staging data.
"""
from django.db import migrations


def pending_signups_are_enabled(apps, schema_editor):
    CustomUser = apps.get_model("users", "CustomUser")
    (CustomUser.objects
     .filter(is_active=False, email_verified=False, last_login__isnull=True,
             is_staff=False, is_superuser=False)
     .exclude(password="")
     .exclude(password__startswith="!")
     .exclude(email__endswith="@invalid.local")
     .update(is_active=True))


def noop(apps, schema_editor):
    """No reverse: after this, an inactive unverified row is a disabled one, and turning
    pending signups back into that shape would strand them again."""


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0038_email_verified_is_not_account_enabled"),
    ]

    operations = [
        migrations.RunPython(pending_signups_are_enabled, noop),
    ]
