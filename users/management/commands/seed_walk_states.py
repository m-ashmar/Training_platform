"""Put ordinary accounts into the states the app's walk-throughs need, and write the
manifest the app's scripts read. DEVELOPMENT ONLY.

    WALK_ACCOUNTS_PASSWORD='<a strong password>' \\
    DB_NAME=<your database> .venv/bin/python manage.py seed_walk_states \\
        --settings=training_platform.settings_local

    ... seed_walk_states --only member_with_post        # reset one state (repeatable)
    ... seed_walk_states --manifest /path/to/walk.json  # write the manifest elsewhere
    ... seed_walk_states --list                         # the states, nothing written
    ... seed_walk_states --keep-going                   # past a failing state, then report

**What it does.** For every registered state (`users/walk/`), in its own transaction:
retire the state's old accounts (moved off their addresses, switched off, nothing
deleted), then make fresh ones at `walk.<state>[.<part>]@yalla.test` and bring them into
the state through the platform's real services. A second run therefore puts every walk
account back into its starting state; ids change, so the scripts read them from the
manifest. The manifest is updated after each state, so it always matches the database.

**What it refuses.** Any settings but the development ones (`settings_local`):
production never runs it. A database that is not on this machine (`DB_HOST` is read from
the environment, so the settings alone would not keep it off a remote server). A missing or weak `WALK_ACCOUNTS_PASSWORD`: the password is
taken from the environment only, and is never printed or written anywhere, the manifest
included. An unknown `--only` state. A database `bootstrap_platform` has not prepared
(`users/walk/framework.py bootstrap_gaps`: the channel capabilities, quota features, a
plan on sale, the diet templates, the badges, the starter exercises, the platform's
templates, the food catalogue), or one missing what the states to be run need of it
(each area's `@needs`, `users/walk/framework.py`: a plan on sale with the diet priced in
the currency a new wallet takes, ...), with ONE message naming everything missing,
before anything is written: the states use that data and never create it, nor an
exchange rate.

**When a state fails.** Its transaction is rolled back: nothing of it changed, and its
old accounts are as they were. By default the run stops there (the states before it are
written and in the manifest). With `--keep-going` it goes on with the next state, writes
every one that succeeds, and at the end names every state that failed with its reason
and exits non-zero.

**What it never touches.** Any account whose address is not `walk.*@yalla.test` — the
app builders' own accounts (`yalla_gym/app/tool/seed_local_backend.py`: rami@, samer@,
new@, agent@, operator@ ...) included — and the server's behaviour at request time:
nothing anywhere reads these addresses, so a walk account is served what any account is.

**The manifest** (`var/walk_states.json` under the project, git-ignored; `--manifest` to
change it)::

    {"generated_at": "...", "domain": "yalla.test",
     "states": {"member_with_post": {
         "name": "member_with_post", "area": "community", "summary": "...",
         "email": "walk.member_with_post@yalla.test", "role": "client",
         "ids": {"user": 41, "post": 7, "comment": 9, "friend": 42},
         "accounts": {"main": {"email": ..., "role": "client", "id": 41},
                      "friend": {"email": "walk.member_with_post.friend@yalla.test",
                                 "role": "client", "id": 42}}}}}

No password and no token, ever.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import translation


class Command(BaseCommand):
    help = ("Development only: reset the walk-through accounts (walk.*@yalla.test) into "
            "their states and write the manifest. Needs WALK_ACCOUNTS_PASSWORD.")

    def add_arguments(self, parser):
        parser.add_argument("--only", action="append", default=[], metavar="STATE",
                            help="Reset this state alone (repeat for several).")
        parser.add_argument("--manifest", default=None,
                            help="Where to write the manifest (default: var/walk_states.json).")
        parser.add_argument("--list", action="store_true",
                            help="List the registered states and change nothing.")
        parser.add_argument("--keep-going", action="store_true",
                            help="Go on past a state that fails; at the end, name every "
                                 "failed state and its reason, and exit non-zero.")

    def handle(self, *args, **options):
        from users.walk import load_states
        from users.walk import framework

        try:
            framework.refuse_unless_development(settings)
            framework.refuse_unless_local_database()
        except framework.WalkRefused as exc:
            raise CommandError(str(exc))
        states = load_states()

        if options["list"]:
            for name in sorted(states):
                spec = states[name]
                self.stdout.write(f"{name:<36} {spec.area:<10} {spec.role:<8} {spec.summary}")
            return

        unknown = sorted(set(options["only"]) - set(states))
        if unknown:
            raise CommandError(f"No such walk state: {', '.join(unknown)}. "
                               f"--list shows them.")
        chosen = options["only"] or sorted(states)
        try:
            password = framework.password_from_environment()
            # Once, before any state: what bootstrap_platform writes and what the chosen
            # states need of this database, every gap named at once. None of them
            # creates platform data itself.
            framework.refuse_unless_ready(chosen)
        except framework.WalkRefused as exc:
            raise CommandError(str(exc))

        path = Path(options["manifest"]) if options["manifest"] else \
            framework.default_manifest_path()
        manifest = framework.read_manifest(path)
        failed = []
        with translation.override(settings.LANGUAGE_CODE):
            for name in chosen:
                try:
                    entry = framework.build(states[name], password)
                except Exception as exc:
                    if not options["keep_going"]:
                        raise CommandError(f"walk state {name} failed, nothing of it was "
                                           f"changed: {exc}") from exc
                    failed.append((name, exc))
                    self.stderr.write(f"{name:<36} FAILED, nothing of it was changed: {exc}")
                    continue
                manifest["states"][name] = entry
                framework.write_manifest(path, manifest)
                self.stdout.write(f"{name:<36} {entry['email']}")
        if failed:
            reasons = "\n".join(f"  {name}: {exc}" for name, exc in failed)
            raise CommandError(
                f"{len(chosen) - len(failed)} walk state(s) reset, {len(failed)} failed "
                f"(nothing of a failed one was changed); manifest: {path}\n{reasons}")
        self.stdout.write(self.style.SUCCESS(
            f"{len(chosen)} walk state(s) reset; manifest: {path}"))
