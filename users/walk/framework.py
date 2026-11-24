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
    that ran it. The command refuses ONCE, before any state, when this is not empty:
    a state never creates platform data (a sellable plan, a catalogue row) itself."""
    from achievements.models import Achievement
    from diet.models import DietPlanTemplate, FoodItem
    from policy.models import ChannelCapabilities
    from routine.data.exercise_catalogue import STARTER_EXERCISES
    from routine.data.platform_templates import PLATFORM_TEMPLATES
    from routine.models import Exercise, RoutineTemplate
    from subscription.models import SubscriptionFeature, SubscriptionPlan

    starter = {exercise.key for exercise in STARTER_EXERCISES}
    templates = {template.key for template in PLATFORM_TEMPLATES}
    checks = (
        ("channel capabilities", ChannelCapabilities.objects.exists()),
        ("quota features", SubscriptionFeature.objects.exists()),
        ("a subscription plan on sale",
         SubscriptionPlan.objects.filter(is_active=True, price__gt=0).exists()),
        # The money states buy a plan whose diet a coach then writes (`coaching.py`).
        ("a subscription plan on sale with the diet",
         SubscriptionPlan.objects.filter(is_active=True, price__gt=0,
                                         has_diet_access=True).exists()),
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


def refuse_unless_bootstrapped() -> None:
    gaps = bootstrap_gaps()
    if gaps:
        raise WalkRefused(
            f"Refusing to run: this database has not been bootstrapped (missing: "
            f"{', '.join(gaps)}). Run `manage.py bootstrap_platform` first; the walk "
            f"states use what it writes and never create it themselves.")


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
        (names, birth date and the client's body and goal), as a new sign-up's is."""
        from datetime import date

        from users.models import CustomUser

        if part is not None and not part.replace("_", "").isalnum():
            raise ValueError(f"walk part {part!r}: letters, digits and _ only")
        key = part or "main"
        if key in self.accounts:
            raise ValueError(f"walk state {self.spec.name}: account {key!r} made twice")
        role = role or (self.spec.role if part is None else "client")
        profile = dict(PROFILES[role]) if onboarded else {}
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
            version: str = "1.0.0", expect=(200, 201)):
        """Call a REAL route as `user`, with the build declared, and answer
        `(status, body)`.

        For what the platform does through a route and no service function — a
        challenge made or joined, a post, a report, a measurement, a goal, a set synced
        the way the phone syncs it. The URL is resolved and its view called with a
        request authenticated as `user` (no token is minted; the middleware stack is
        not run). A status outside `expect` raises `WalkApiRefused` with the body, so a
        state never builds on a refusal it did not mean."""
        from django.urls import resolve
        from rest_framework.test import APIRequestFactory, force_authenticate

        factory = APIRequestFactory()
        headers = {"HTTP_X_APP_CHANNEL": build, "HTTP_X_APP_VERSION": version,
                   "HTTP_ACCEPT_LANGUAGE": "ar"}
        maker = getattr(factory, method.lower())
        if method.lower() == "get":
            request = maker(path, data or {}, **headers)
        else:
            request = maker(path, data or {}, format="json", **headers)
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
