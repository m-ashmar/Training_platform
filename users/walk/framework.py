"""The machinery behind `manage.py seed_walk_states` (see `users/walk/__init__.py`).

**Reset, not delete.** A walk pays, accepts, confirms and logs, so a second run must put
every walk account back into its starting state — and the ledger and event tables are
append-only (their rows are never deleted, no trigger is ever bypassed). So a reset
RETIRES the state's old accounts and makes fresh ones under the same addresses:

* the old account is moved off its address (`walk.retired.<id>.<tag>@yalla.test`), its
  username likewise, made inactive (so it is shown nowhere: `users/visibility.py`), its
  password made unusable and every session ended (`users/utils.py end_every_session`);
* nothing it wrote is deleted: its ledger rows, payments, relations and notices stay
  attached to the retired account, exactly as an erased or suspended account's do;
* `on_retire` hooks (an area's, for walk data the visibility rule does not already hide)
  run in the same transaction;
* then the state's function makes fresh accounts under the same addresses.

Ids therefore change on every run: the walk scripts read them from the manifest.

**Only `walk.*` accounts.** The command finds a state's accounts by their address
(`walk.<state>@yalla.test`, `walk.<state>.<part>@yalla.test`) and touches no other.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Callable

DOMAIN = "yalla.test"
PREFIX = "walk."
PASSWORD_ENV = "WALK_ACCOUNTS_PASSWORD"

#: The development settings: the only module the command runs under.
DEVELOPMENT_SETTINGS = "training_platform.settings_local"

#: Run in a state's transaction when its old accounts are retired, before the fresh ones
#: are made: `hook(old_users)`. For walk data the visibility rule does not hide on its
#: own (registered by an area module with `@on_retire`).
RETIRE_HOOKS: list[Callable] = []


def on_retire(hook):
    RETIRE_HOOKS.append(hook)
    return hook


#: What states need of THIS database beyond bootstrap's own steps, declared by their area
#: modules with `@needs(...)`: `(check, state names, area)`. A check answers what is
#: missing, one sentence per gap (`[]` when nothing is), and must only READ.
NEEDS: list[tuple[Callable, frozenset, str | None]] = []


def needs(*state_names: str, area: str | None = None):
    """Declare what some states need of the database, checked ONCE before any state is
    built (`refuse_unless_ready`), with every other gap, so a run names everything
    missing at once and writes nothing::

        @needs("premium_no_diet_plan")
        def _a_diet_plan_a_new_wallet_can_pay():
            return [] if ... else ["a plan on sale with the diet, priced in SYP"]

    Names: the check applies when one of these states is run. `area`: when any state of
    that area is. Neither: always. A gap is said as what is missing, in the words an
    operator would act on: "a plan on sale with the diet, priced in SYP (the currency a
    new wallet takes)", never "plan lookup failed"."""
    def register(check):
        NEEDS.append((check, frozenset(state_names), area))
        return check
    return register


class WalkRefused(Exception):
    """The command must not run; the message says why."""


class WalkApiRefused(Exception):
    """A route a state called answered what the state did not expect: the state's
    transaction is rolled back and the command says which route answered what."""


# ------------------------------------------------------------------ the guards
def refuse_unless_development(settings) -> None:
    """Only under the development settings: never production, never anything else."""
    module = getattr(settings, "SETTINGS_MODULE", "") or os.environ.get(
        "DJANGO_SETTINGS_MODULE", "")
    if module != DEVELOPMENT_SETTINGS:
        raise WalkRefused(
            f"Refusing to run: seed_walk_states is development-only, and these settings "
            f"are {module or 'unknown'!s} (only {DEVELOPMENT_SETTINGS}).")


#: Database hosts on this machine: a loopback address, or a socket (an empty host).
LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})


def refuse_unless_local_database() -> None:
    """The development settings take the database from the environment (`DB_HOST`,
    `DB_NAME`), so the settings module alone does not keep this command off a remote
    server: `DB_HOST=<anything>` would point it there. Refused unless the database is on
    this machine — a loopback host, a `.localhost` name, or a Unix socket."""
    from django.db import connections

    host = (connections["default"].settings_dict.get("HOST") or "").strip()
    if host in LOCAL_HOSTS or host.startswith("/") or host.endswith(".localhost"):
        return
    raise WalkRefused(f"Refusing to run: the database host is {host!r}, not this machine. "
                      f"seed_walk_states writes only to a local development database.")


def password_from_environment() -> str:
    """The walk accounts' one password: from the environment, never the code, never
    printed or written anywhere."""
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    password = os.environ.get(PASSWORD_ENV, "")
    if not password:
        raise WalkRefused(f"Refusing to run: set {PASSWORD_ENV}. The walk accounts' "
                          f"password comes from the environment, never from the code.")
    try:
        validate_password(password)
    except ValidationError as exc:
        raise WalkRefused(f"{PASSWORD_ENV} is not strong enough: " + " ".join(exc.messages))
    return password


def bootstrap_gaps() -> list[str]:
    """What `bootstrap_platform` writes and is missing here, one entry per step the
    states rely on (its own steps, read from its own sources). Empty on a dev server
    that ran it. A state never creates platform data (a sellable plan, a catalogue row)
    itself.

    Plans are not here: which plan a state can use depends on the CURRENCY its client
    trades in, and "a plan on sale" was true of a database whose only one was in dollars
    (the developers', 2026-11-24). Each state that buys or prices a plan declares the
    plan it needs, by currency, with `@needs` (below, and `diet.py`)."""
    from achievements.models import Achievement
    from diet.models import DietPlanTemplate, FoodItem
    from policy.models import ChannelCapabilities
    from routine.data.exercise_catalogue import STARTER_EXERCISES
    from routine.data.platform_templates import PLATFORM_TEMPLATES
    from routine.models import Exercise, RoutineTemplate
    from subscription.models import SubscriptionFeature

    starter = {exercise.key for exercise in STARTER_EXERCISES}
    templates = {template.key for template in PLATFORM_TEMPLATES}
    checks = (
        ("channel capabilities", ChannelCapabilities.objects.exists()),
        ("quota features", SubscriptionFeature.objects.exists()),
        ("diet plan templates", DietPlanTemplate.objects.exists()),
        ("achievements", Achievement.objects.exists()),
        ("starter exercises",
         Exercise.objects.filter(catalogue_key__in=starter).count() == len(starter)),
        ("platform templates",
         RoutineTemplate.objects.filter(platform_key__in=templates).count()
         == len(templates)),
        ("food catalogue", FoodItem.objects.filter(needs_review=False).exists()),
    )
    return [name for name, present in checks if not present]


def state_needs(names=None) -> list[str]:
    """What the states named (every registered state when None) need of this database
    and is missing (`@needs`), in registration order, each gap once."""
    from . import load_states

    states = load_states()
    chosen = set(states) if names is None else set(names)
    areas = {states[name].area for name in chosen if name in states}
    gaps: list[str] = []
    for check, for_states, for_area in NEEDS:
        applies = ((not for_states and for_area is None) or bool(for_states & chosen)
                   or (for_area is not None and for_area in areas))
        if applies:
            gaps.extend(gap for gap in check() if gap not in gaps)
    return gaps


def refuse_unless_ready(names=None) -> None:
    """ONCE, before any state: what `bootstrap_platform` writes, and what the states to
    be run need of this database (`@needs`). Every gap is named in one refusal, and
    nothing is written."""
    missing = bootstrap_gaps()
    wanted = state_needs(names)
    if not missing and not wanted:
        return
    parts = []
    if missing:
        parts.append(
            f"this database has not been bootstrapped (missing: {', '.join(missing)}). "
            f"Run `manage.py bootstrap_platform` first; the walk states use what it writes "
            f"and never create it themselves")
    if wanted:
        parts.append(
            f"the walk states to be run cannot run on this database and these settings: "
            f"{'; '.join(wanted)}. The states never create platform data or a rate "
            f"themselves")
    raise WalkRefused("Refusing to run, nothing was written: " + ". And ".join(parts) + ".")


# ------------------------------------------------------------------ addresses
def address(state_name: str, part: str | None = None) -> str:
    local = f"{PREFIX}{state_name}" + (f".{part}" if part else "")
    return f"{local}@{DOMAIN}"


def username(state_name: str, part: str | None = None) -> str:
    return f"walk_{state_name}" + (f"_{part}" if part else "")


def accounts_of(state_name: str):
    """Every account a state owns now: its main address and its parts' addresses."""
    from django.db.models import Q

    from users.models import CustomUser

    return CustomUser.objects.filter(
        Q(email=address(state_name))
        | Q(email__startswith=f"{PREFIX}{state_name}.", email__endswith=f"@{DOMAIN}"))


def is_walk_address(email: str) -> bool:
    return bool(email) and email.startswith(PREFIX) and email.endswith(f"@{DOMAIN}")


def retire(users) -> int:
    """Move each account off its address and switch it off (the module docstring)."""
    from users.utils import end_every_session

    users = list(users)
    for hook in RETIRE_HOOKS:
        hook(users)
    for user in users:
        tag = uuid.uuid4().hex[:8]
        user.email = f"{PREFIX}retired.{user.pk}.{tag}@{DOMAIN}"
        user.username = f"walk_retired_{user.pk}_{tag}"
        user.is_active = False
        user.set_unusable_password()
        user.save(update_fields=["email", "username", "is_active", "password"])
        end_every_session(user)
    return len(users)


# ------------------------------------------------------------------ profiles
#: What every fresh account starts with, by role: a finished profile (the app's
#: onboarding asks for nothing) unless a state passes `onboarded=False`.
PROFILES = {
    "client": dict(first_name="سامي", last_name="عابد", date_of_birth="1997-04-12",
                   gender="Male", height=176, weight=78, activity_level="Moderate",
                   training_goal="build_muscle", training_place="gym",
                   training_days_per_week=4),
    "trainer": dict(first_name="هادي", last_name="سلّوم", date_of_birth="1990-02-20",
                    gender="Male",
                    trainer_bio="أدرّب على القوة وبناء العضل، وأتابع كل متدرب أسبوعياً.",
                    trainer_specializations=["قوة", "بناء عضل"],
                    trainer_certifications=["مدرب شخصي معتمد"],
                    trainer_experience_years=6, trainer_goal_focus=["build_muscle"],
                    trainer_training_places=["gym"], trainer_is_available=True),
    "agent": dict(first_name="وليد", last_name="الصرّاف", date_of_birth="1986-08-01"),
    "admin": dict(first_name="منى", last_name="المشرفة", date_of_birth="1988-12-05"),
}


class Walk:
    """What a state's function is handed: its accounts, the clock, and the manifest
    entry it fills."""

    def __init__(self, spec, password: str):
        self.spec = spec
        self._password = password
        self.accounts: dict[str, dict] = {}
        self._ids: dict[str, object] = {}

    # The state's accounts ---------------------------------------------------
    def account(self, part: str | None = None, *, role: str | None = None,
                onboarded: bool = True, **fields):
        """A FRESH account at `walk.<state>[.<part>]@yalla.test`, signed in with the walk
        password, its address verified (the operator's path, `create_user`). `role`
        defaults to the state's own for the main account and to `client` for a part.
        `fields` override the role's profile; `onboarded=False` leaves the profile empty
        (names, birth date and the client's body and goal), as a new sign-up's is.

        The saved language (`preferred_language`, the language of the account's pushes,
        e-mails and the sentences written for it) is Arabic, as a sign-up from an Arabic
        phone saves it (`users/serializers.py SIGNUP_DEFAULT_LANGUAGE`); `create_user`
        alone leaves the column default, English. A state that needs an English account
        passes `preferred_language="en"`."""
        from datetime import date

        from users.models import CustomUser

        if part is not None and not part.replace("_", "").isalnum():
            raise ValueError(f"walk part {part!r}: letters, digits and _ only")
        key = part or "main"
        if key in self.accounts:
            raise ValueError(f"walk state {self.spec.name}: account {key!r} made twice")
        role = role or (self.spec.role if part is None else "client")
        from users.serializers import SIGNUP_DEFAULT_LANGUAGE

        profile = dict(PROFILES[role]) if onboarded else {}
        profile["preferred_language"] = SIGNUP_DEFAULT_LANGUAGE
        profile.update(fields)
        if isinstance(profile.get("date_of_birth"), str):
            profile["date_of_birth"] = date.fromisoformat(profile["date_of_birth"])
        user = CustomUser.objects.create_user(
            email=address(self.spec.name, part), username=username(self.spec.name, part),
            password=self._password, user_type=role)
        for name, value in profile.items():
            setattr(user, name, value)
        user.save()
        self.accounts[key] = {"email": user.email, "role": role, "id": user.pk}
        return user

    # The real routes, for what has no service function ------------------------
    def api(self, user, method: str, path: str, data=None, *, build: str = "apk",
            version: str = "1.0.0", expect=(200, 201), multipart: bool = False):
        """Call a REAL route as `user`, with the build declared, and answer
        `(status, body)`.

        For what the platform does through a route and no service function — a
        challenge made or joined, a post, a report, a measurement, a goal, a set synced
        the way the phone syncs it. The URL is resolved and its view called with a
        request authenticated as `user` (no token is minted; the middleware stack is
        not run). A status outside `expect` raises `WalkApiRefused` with the body, so a
        state never builds on a refusal it did not mean. `multipart=True` sends the body
        as a form, for an upload (a `SimpleUploadedFile` in `data`), as the app does."""
        from django.urls import resolve
        from rest_framework.test import APIRequestFactory, force_authenticate

        factory = APIRequestFactory()
        headers = {"HTTP_X_APP_CHANNEL": build, "HTTP_X_APP_VERSION": version,
                   "HTTP_ACCEPT_LANGUAGE": "ar"}
        maker = getattr(factory, method.lower())
        if method.lower() == "get":
            request = maker(path, data or {}, **headers)
        else:
            request = maker(path, data or {}, format="multipart" if multipart else "json",
                            **headers)
        force_authenticate(request, user=user)
        match = resolve(path.split("?")[0])
        response = match.func(request, *match.args, **match.kwargs)
        if hasattr(response, "render"):
            response.render()
        body = json.loads(response.content) if response.content else None
        if response.status_code not in expect:
            raise WalkApiRefused(f"{method.upper()} {path} as {user.email} answered "
                                 f"{response.status_code}: {body}")
        return response.status_code, body

    # What the walk scripts read ---------------------------------------------
    def ids(self, **ids) -> None:
        """Record ids the walk needs (`trainer=`, `routine=`, `plan=`, `challenge=`,
        `request=`, `payment=` ...). Values must be JSON: an id, or a list of ids."""
        for name, value in ids.items():
            json.dumps(value)
            self._ids[name] = value

    def entry(self) -> dict:
        main = self.accounts.get("main")
        if main is None:
            raise ValueError(f"walk state {self.spec.name} made no main account "
                             f"(walk.account())")
        return {
            "name": self.spec.name,
            "area": self.spec.area,
            "summary": self.spec.summary,
            "email": main["email"],
            "role": main["role"],
            "ids": {"user": main["id"], **self._ids},
            "accounts": self.accounts,
        }


def build(spec, password: str) -> dict:
    """Reset one state: retire its old accounts, run its function, answer its manifest
    entry. One transaction: a state that fails leaves its old accounts as they were."""
    from django.db import transaction

    with transaction.atomic():
        retire(accounts_of(spec.name).select_for_update())
        walk = Walk(spec, password)
        spec.build(walk)
        return walk.entry()


# ------------------------------------------------------------------ the manifest
def default_manifest_path() -> Path:
    from django.conf import settings

    return Path(settings.BASE_DIR) / "var" / "walk_states.json"


def read_manifest(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"states": {}}
    if not isinstance(data, dict) or not isinstance(data.get("states"), dict):
        return {"states": {}}
    return data


def write_manifest(path: Path, data: dict) -> None:
    """Written whole, through a temporary file, so a script never reads half of it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["generated_at"] = datetime.now(dt_timezone.utc).isoformat()
    data["domain"] = DOMAIN
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                         encoding="utf-8")
    temporary.replace(path)


# ------------------------------------------------------------------ what the states need
# Declared here, with the framework's own checks, at the manager's ruling (2026-11-24):
# what the money worker's states need (`coaching.py`, `wallet.py`) and what the
# environment must allow for any state to move money. An area may declare its own beside
# its states instead (`diet.py` does). Each check only reads, and says each gap in the
# words an operator acts on.

@needs("coaching_frozen", "plan_lapsed")
def _a_plan_with_the_diet_a_new_wallet_can_pay():
    """These buy a plan whose diet a coach then writes, from a new client's wallet."""
    from .shared import missing_plan

    return missing_plan(diet=True)


@needs("plan_not_renewing", "plan_renewal_due", "client_wallet_covers_plan")
def _a_plan_a_new_wallet_can_pay():
    """These buy or price a plan from a new client's wallet."""
    from .shared import missing_plan

    return missing_plan()


def _base_suffix(currency: str) -> str:
    """The settings name a currency's twin by: the pound caps are unsuffixed, the
    others carry `_<CODE>` (`wallet.models.ensure_agent_profile`)."""
    return "" if currency == "SYP" else f"_{currency}"


@needs()
def _a_new_agent_may_take_cash_in_the_base_currency():
    """Every state that tops a wallet up does it at a new agent, whose caps are the
    settings' defaults (`ensure_agent_profile`). A daily or monthly cap of 0, or a set
    per-top-up cap or ceiling of 0, means that agent takes no cash at all."""
    from decimal import Decimal

    from django.conf import settings

    from .shared import new_wallet_currency

    currency = new_wallet_currency()
    suffix = _base_suffix(currency)
    gaps = []
    for name, required in (("AGENT_DEFAULT_DAILY_LIMIT", True),
                           ("AGENT_DEFAULT_MONTHLY_LIMIT", True),
                           ("AGENT_DEFAULT_PER_TRANSACTION_LIMIT", False),
                           ("AGENT_DEFAULT_RECEIVABLE_CEILING", False)):
        value = getattr(settings, name + suffix, None)
        if value in (None, "") and not required:
            continue            # unset: no cap of its own, as ensure_agent_profile reads it
        if value in (None, "") or Decimal(str(value)) <= 0:
            gaps.append(f"the setting {name + suffix} above 0 (a new agent's {currency} cap: "
                        f"at 0 it takes no cash)")
    return gaps


@needs("trainer_withdrawal_requested", "trainer_withdrawal_sent",
       "trainer_withdrawal_refused", "inbox_every_kind")
def _withdrawals_in_the_base_currency():
    """These request withdrawals in a new trainer's currency, which `payouts.services`
    refuses without a minimum and a daily cap set for it."""
    from decimal import Decimal

    from django.conf import settings

    from .shared import new_wallet_currency

    currency = new_wallet_currency()
    gaps = []
    if (getattr(settings, "PAYOUT_MIN_AMOUNT", {}) or {}).get(currency) is None:
        gaps.append(f"a {currency} entry in the setting PAYOUT_MIN_AMOUNT")
    cap = (getattr(settings, "PAYOUT_DAILY_CAP", {}) or {}).get(currency)
    if cap is None or Decimal(str(cap)) <= 0:
        gaps.append(f"a {currency} entry above 0 in the setting PAYOUT_DAILY_CAP")
    return gaps
