"""The demo accounts the app-store reviewers sign in with (spec section 10; section 9 B8).

    REVIEW_ACCOUNTS_EMAIL='appreview+{role}@your-domain' \\
    REVIEW_ACCOUNTS_PASSWORD='<a strong password>' \\
    python manage.py seed_review_accounts

    python manage.py seed_review_accounts --email 'review-{role}@your-domain'
    python manage.py seed_review_accounts --list

**What a reviewer finds.** The spec asks for "one client on the Premium plan with a
trainer, an active routine, two weeks of logged workouts, a meal plan and wallet history;
one verified trainer with three clients and a pending request; data that looks real and
contains no real person's details." So, by role:

| role | account |
|---|---|
| `client` | the reviewer's client: Premium (an operator grant), coached by `trainer` (an approved month, paid from the wallet), a routine with two weeks of sessions and set logs, a 7-day meal plan, wallet history (an agent top-up, the coaching month), a running challenge |
| `trainer` | verified by an operator decision; three approved clients (`client`, `client2`, `client3`) and one pending request (`prospect`); a template and a routine per client |
| `agent` | an agent with the platform's default caps (`AGENT_DEFAULT_*`), who made the top-ups |
| `client2`, `client3` | the trainer's other two clients |
| `prospect` | a client whose request is waiting for the trainer |

The names are invented and the phone numbers are the model's placeholder: no real
person's details. Every account is in the group `REVIEW_GROUP`, which is how they are
found (the console's user list filters by group) and removed. **Nothing reads that group
but this command** — rule zero: behaviour may depend on the build and nothing else, so a
review account is served exactly what any account is (`tests/test_bootstrap_commands.py`
holds the source to it).

**Where the addresses and the password come from: the environment, never this file.**

* `REVIEW_ACCOUNTS_EMAIL` (or `--email`): a mailbox the owner controls. With `{role}` in
  it, the role is put there (`review-{role}@x.com` -> `review-client@x.com`); without,
  it is plus-addressed (`review@x.com` -> `review+client@x.com`). Six distinct addresses.
* `REVIEW_ACCOUNTS_PASSWORD`: one password for the six, checked against
  `AUTH_PASSWORD_VALIDATORS`. Not an argument, deliberately: an argument is visible in
  the process list and saved in shell history. Run again with a new value to rotate it.

The command refuses, before writing anything, when either is missing, when the
password is weak, when an address or username already belongs to an account that is
NOT a review account (it never takes over a person's account), and when the database
has not been bootstrapped (`bootstrap_platform`).

**How the data is made: the platform's own paths**, so every invariant holds —
`create_user` (the operator's path: an address the operator vouches for is verified;
this is not the OTP sign-up, which a person uses), the verification service
(`upload_document` / `submit` / `decide`: placeholder images that SAY they are not
identity documents, and an operator decision saying why), `ensure_agent_profile` and the
agent top-up's ledger write and receivable entry, the coaching request and
`approve_trainer_relation` (the hold) and `assign_routine` (month 1 paid out, the 20%
commission taken), `routine_from_template`, the operator grant (`source=operator_grant`,
as the console's "activate" writes it — no payment row, because no money moved),
`generate_and_save` for the meal plan, and the challenge join's recount and ranking.
Only the two weeks of history are written as rows: nothing but a phone logs a set, so
there is no service to call. They are written as the app would have written them on
each day, `created_at` included, so the challenge's anti-cheat counts them.

**Idempotent.** Each piece is found before it is made; a second run writes nothing. Two
things are kept current when the command runs again later, because a reviewer may look
weeks after the first run: the Premium grant is extended when fewer than
`GRANT_TOP_UP_DAYS` remain, and a new 7-day meal plan is generated when none covers
today. Run it again shortly before each submission.

Safe on production: no setting it needs is development-only, no secret is printed, and
it moves no money it did not first put in through an agent top-up.
"""
from __future__ import annotations

import io
import os
import random
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone, translation

#: The marker. Read by nothing but this command (see the module docstring).
REVIEW_GROUP = "Store review accounts"

EMAIL_ENV = "REVIEW_ACCOUNTS_EMAIL"
PASSWORD_ENV = "REVIEW_ACCOUNTS_PASSWORD"

#: How long the Premium grant runs, and how little of it may be left before a later run
#: extends it.
GRANT_DAYS = 90
GRANT_TOP_UP_DAYS = 30

#: What each account is. Names are invented; `date_of_birth` puts every one well above
#: the platform's minimum age.
PEOPLE = {
    "client": dict(user_type="client", first_name="سامر", last_name="حداد",
                   date_of_birth="1996-03-14", gender="Male", height=178, weight=82,
                   activity_level="Moderate", training_goal="build_muscle",
                   training_place="gym", training_days_per_week=4),
    "trainer": dict(user_type="trainer", first_name="رامي", last_name="منصور",
                    date_of_birth="1990-05-02", gender="Male",
                    trainer_bio="أدرّب منذ ثماني سنوات، وأركّز على بناء القوة بطريقة آمنة. "
                                "أكتب لكل متدرب برنامجاً يناسب وقته ومعداته، وأراجع أداءه "
                                "أسبوعاً بأسبوع.",
                    trainer_specializations=["قوة", "بناء عضل"],
                    trainer_certifications=["مدرب شخصي معتمد", "تغذية رياضية"],
                    trainer_experience_years=8, trainer_goal_focus=["build_muscle", "strength"],
                    trainer_training_places=["gym"],
                    # Not accepting new clients: this is a demonstration trainer on the
                    # production directory, and a real client must not be able to send
                    # it a request and pay for a month nobody will coach. The switch is
                    # the one every trainer has (`trainer_not_accepting`); nothing about
                    # it is specific to review accounts.
                    trainer_is_available=False),
    "agent": dict(user_type="agent", first_name="نبيل", last_name="سعيد",
                  date_of_birth="1985-11-20"),
    "client2": dict(user_type="client", first_name="ليلى", last_name="عيسى",
                    date_of_birth="1998-07-09", gender="Female", height=165, weight=60,
                    activity_level="Light", training_goal="fat_loss",
                    training_place="gym", training_days_per_week=3),
    "client3": dict(user_type="client", first_name="كريم", last_name="يوسف",
                    date_of_birth="1994-01-25", gender="Male", height=182, weight=90,
                    activity_level="Active", training_goal="strength",
                    training_place="gym", training_days_per_week=4),
    "prospect": dict(user_type="client", first_name="مايا", last_name="خليل",
                     date_of_birth="2000-10-03", gender="Female", height=160, weight=58,
                     activity_level="Moderate", training_goal="general_fitness",
                     training_place="home", training_days_per_week=3),
}
COACHED = ("client", "client2", "client3")

#: Money, per currency the platform may run in (`PLATFORM_CURRENCY`): the trainer's
#: monthly fee, and what the agent tops each client up with before they ask — enough for
#: month 1 and, for `client`, a renewal. Well inside the default agent caps, so the
#: reviewer can still make a top-up of their own from the agent account the same day.
MONEY = {
    "SYP": {"fee": Decimal("20000.00"),
            "topups": {"client": Decimal("50000.00"), "client2": Decimal("25000.00"),
                       "client3": Decimal("25000.00"), "prospect": Decimal("25000.00")}},
    "USD": {"fee": Decimal("15.00"),
            "topups": {"client": Decimal("40.00"), "client2": Decimal("20.00"),
                       "client3": Decimal("20.00"), "prospect": Decimal("20.00")}},
}

#: The trainer's 4-day program, from the starter catalogue (`routine/data/
#: exercise_catalogue.py`): (catalogue key, sets, reps, rest seconds, starting kg or None
#: for bodyweight).
PROGRAM = {
    1: [("barbell-bench-press", 4, 8, 90, 60), ("seated-dumbbell-shoulder-press", 3, 10, 75, 16),
        ("incline-dumbbell-press", 3, 10, 75, 22), ("dumbbell-lateral-raise", 3, 15, 45, 8),
        ("triceps-rope-pushdown", 3, 12, 60, 25)],
    2: [("lat-pulldown", 4, 10, 75, 50), ("barbell-bent-over-row", 4, 8, 90, 55),
        ("face-pull", 3, 15, 45, 20), ("barbell-curl", 3, 12, 60, 30),
        ("hammer-curl", 3, 12, 60, 14)],
    3: [("barbell-back-squat", 4, 8, 120, 80), ("barbell-romanian-deadlift", 3, 10, 90, 60),
        ("leg-press", 3, 12, 90, 120), ("lying-leg-curl", 3, 12, 60, 35),
        ("standing-calf-raise", 4, 15, 45, 60)],
    4: [("incline-barbell-bench-press", 4, 8, 90, 50), ("pull-up", 3, 8, 90, None),
        ("arnold-press", 3, 10, 75, 14), ("seated-cable-row", 3, 12, 75, 50),
        ("crunch", 3, 20, 45, None)],
}
DAY_NAMES = {"1": "دفع · صدر وأكتاف", "2": "سحب · ظهر وذراعان", "3": "أرجل",
             "4": "الجزء العلوي"}
TEMPLATE = {"name_en": "4-Day Strength Program", "name_ar": "برنامج القوة · 4 أيام",
            "description_en": "Strength and muscle building, four days a week.",
            "description_ar": "برنامج قوة وبناء عضل، أربعة أيام في الأسبوع.",
            "goal": "Strength"}
#: The weekdays `client` trained (Mon, Tue, Thu, Sat -> routine days 1-4).
TRAINING_WEEKDAYS = {0: 1, 1: 2, 3: 3, 5: 4}

CHALLENGE = {"title_en": "100 Sets in Two Weeks", "title_ar": "تحدي المئة مجموعة",
             "description_en": "Log 100 sets in two weeks, on any exercise.",
             "description_ar": "سجّل مئة مجموعة خلال أسبوعين، بأي تمرين."}
#: `rules` is not a translated column; the app is Arabic-first.
CHALLENGE_RULES = "كل مجموعة مسجّلة في التطبيق تُحتسب."

VERIFICATION_NOTE = ("Store-review demo account, created by manage.py "
                     "seed_review_accounts. The documents are placeholder images that say "
                     "so; no real person's identity is on file.")


class Command(BaseCommand):
    help = ("Create or refresh the app-store reviewers' demo accounts. Needs "
            f"{EMAIL_ENV} (or --email) and {PASSWORD_ENV}.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            help=f"The address pattern, instead of {EMAIL_ENV}: '{{role}}' is replaced "
                 "by the role, otherwise the address is plus-addressed.")
        parser.add_argument(
            "--list", action="store_true",
            help="List the review accounts that exist, and change nothing.")

    # ------------------------------------------------------------------ entry
    def handle(self, *args, **options):
        if options["list"]:
            return self._list()
        pattern = (options.get("email") or os.environ.get(EMAIL_ENV, "")).strip()
        password = os.environ.get(PASSWORD_ENV, "")
        missing = [name for name, value in ((EMAIL_ENV, pattern), (PASSWORD_ENV, password))
                   if not value]
        if missing:
            raise CommandError(
                f"Refusing to run: set {' and '.join(missing)}. The review accounts' "
                f"addresses and password come from the environment (or --email for the "
                f"address), never from the code.")
        self.addresses = self._addresses(pattern)
        self._check_password(password)
        self.password = password
        self._check_prerequisites()
        self._check_nobody_else_owns_these()

        self.log = []
        with translation.override(settings.LANGUAGE_CODE):
            self.users = {role: self._account(role) for role in PEOPLE}
            self._verify_trainer()
            self._agent()
            self._top_ups()
            self._coaching()
            self._routines()
            self._premium()
            self._history()
            self._meal_plan()
            self._challenge()

        width = max(len(item) for item, _outcome in self.log)
        for item, outcome in self.log:
            self.stdout.write(f"{item.ljust(width)}  {outcome}")
        self.stdout.write("")
        for role, user in self.users.items():
            self.stdout.write(f"{role:<9} {user.email}")
        written = sum(1 for _item, outcome in self.log if outcome != "found")
        self.stdout.write(self.style.SUCCESS(
            f"review accounts ready: {written} change(s)" if written
            else "review accounts ready: nothing to change"))

    def _list(self):
        from users.models import CustomUser

        people = (CustomUser.objects.filter(groups__name=REVIEW_GROUP)
                  .order_by("id").values_list("email", "user_type", "is_active"))
        for email, user_type, active in people:
            self.stdout.write(f"{email}  {user_type}  {'active' if active else 'inactive'}")
        if not people:
            self.stdout.write("no review accounts")

    def _note(self, item, outcome):
        self.log.append((item, outcome))

    # ---------------------------------------------------------- the refusals
    def _addresses(self, pattern) -> dict:
        addresses = {}
        for role in PEOPLE:
            if "{role}" in pattern:
                address = pattern.replace("{role}", role)
            else:
                local, at, domain = pattern.partition("@")
                address = f"{local}+{role}@{domain}" if at else ""
            try:
                validate_email(address)
            except ValidationError:
                raise CommandError(
                    f"{EMAIL_ENV} must be an address such as review@example.com or "
                    f"review-{{role}}@example.com; it gave {address!r} for {role}.")
            addresses[role] = address.lower()
        if len(set(addresses.values())) != len(addresses):
            raise CommandError(f"{EMAIL_ENV} must give each role its own address.")
        return addresses

    def _check_password(self, password):
        from users.models import CustomUser

        probe = CustomUser(email=self.addresses["client"], username="review_client")
        try:
            validate_password(password, user=probe)
        except ValidationError as exc:
            raise CommandError(f"{PASSWORD_ENV} is not strong enough: "
                               + " ".join(exc.messages))

    def _check_prerequisites(self):
        from diet.models import FoodItem
        from routine.models import Exercise
        from subscription.default_plans import PREMIUM_PLAN_NAME
        from subscription.models import SubscriptionPlan

        keys = {key for day in PROGRAM.values() for key, *_rest in day}
        gaps = []
        if not SubscriptionPlan.objects.filter(name_en=PREMIUM_PLAN_NAME).exists():
            gaps.append(f"the plan {PREMIUM_PLAN_NAME!r}")
        if Exercise.objects.filter(catalogue_key__in=keys).count() != len(keys):
            gaps.append("the starter exercises")
        if not FoodItem.objects.exists():
            gaps.append("the food catalogue")
        if gaps:
            raise CommandError(f"Refusing to run: this database has no {', no '.join(gaps)}. "
                               f"Run `manage.py bootstrap_platform` first.")

    def _check_nobody_else_owns_these(self):
        """Never take over a person's account: an address or a username already in use
        by an account outside the review group stops the command."""
        from django.db.models import Q

        from users.models import CustomUser

        for role, address in self.addresses.items():
            taken = (CustomUser.objects
                     .filter(Q(email__iexact=address) | Q(username=f"review_{role}"))
                     .exclude(groups__name=REVIEW_GROUP))
            if taken.exists():
                raise CommandError(
                    f"Refusing to run: {address} or the username review_{role} belongs to "
                    f"an account that is not a review account. Choose another address.")
            mine = CustomUser.objects.filter(groups__name=REVIEW_GROUP,
                                             username=f"review_{role}").first()
            if mine is not None and mine.email.lower() != address:
                raise CommandError(
                    f"Refusing to run: the review {role} already exists as {mine.email}, "
                    f"not {address}. Run with the address it was created with.")

    # ------------------------------------------------------------- accounts
    def _account(self, role):
        from users.models import CustomUser

        group, _created = Group.objects.get_or_create(name=REVIEW_GROUP)
        with transaction.atomic():
            user = CustomUser.objects.filter(username=f"review_{role}",
                                             groups=group).first()
            if user is None:
                profile = dict(PEOPLE[role])
                profile["date_of_birth"] = datetime.strptime(
                    profile["date_of_birth"], "%Y-%m-%d").date()
                # The operator's path (`CustomUserManager.create_user`): an address the
                # platform vouches for is verified. Not the OTP sign-up, which is how a
                # PERSON proves an address is theirs.
                user = CustomUser.objects.create_user(
                    email=self.addresses[role], username=f"review_{role}",
                    password=self.password, preferred_language="ar", **profile)
                user.groups.add(group)
                self._note(f"account {role}", "created")
                return user
            if not user.check_password(self.password):
                user.set_password(self.password)
                user.save(update_fields=["password"])
                self._note(f"account {role}", "password changed")
            else:
                self._note(f"account {role}", "found")
            return user

    def _verify_trainer(self):
        """Through the verification service, as an operator would: the trainer's
        documents, the submission, the decision. The documents are placeholder
        images that say they are not identity documents."""
        from django.core.files.base import ContentFile

        from trainer_verification import services, slots
        from trainer_verification.models import ApplicationState

        trainer = self.users["trainer"]
        if services.status_for(trainer) == ApplicationState.VERIFIED:
            return self._note("trainer verification", "found")
        application = services.application_of(trainer)
        if application is None or application.state != ApplicationState.PENDING:
            for index, slot in enumerate(slots.REQUIRED):
                image = ContentFile(_placeholder_document(slot, index),
                                    name=f"{slot}.png")
                services.upload_document(trainer, slot, image)
            application = services.submit(trainer)
        services.decide(application.pk, None, approve=True, reason=VERIFICATION_NOTE)
        trainer.refresh_from_db()
        self._note("trainer verification", "verified by an operator decision")

    def _agent(self):
        from wallet.models import AgentProfile, ensure_agent_profile

        agent = self.users["agent"]
        if AgentProfile.objects.filter(user=agent).exists():
            return self._note("agent profile", "found")
        profile = ensure_agent_profile(agent)
        # Not listed: the public "agents near you" list is where real clients find a
        # shop holding cash, and this one holds none.
        profile.display_name = "وكيل تجريبي"
        profile.city, profile.area = "دمشق", "المزة"
        profile.is_listed = False
        profile.save(update_fields=["display_name", "city", "area", "is_listed",
                                    "updated_at"])
        self._note("agent profile", "created with the default caps")

    # ----------------------------------------------------------------- money
    def _money(self):
        from wallet import currency_for

        currency = currency_for(self.users["client"])
        if currency not in MONEY:
            raise CommandError(f"No demo amounts for the currency {currency}.")
        return currency, MONEY[currency]

    def _top_ups(self):
        """The agent tops each client up once, as an agent top-up writes it: the
        ledger row, and the receivable entry for the cash the agent now holds."""
        from policy.money import MoneyType
        from wallet import agents
        from wallet.models import AgentProfile, Transaction, move_funds_atomic, wallet_for

        currency, money = self._money()
        agent = self.users["agent"]
        for role, amount in money["topups"].items():
            client = self.users[role]
            wallet = wallet_for(client, "client")
            if Transaction.objects.filter(actor_id=agent.id, tx_type="topup",
                                          destination_wallet=wallet).exists():
                self._note(f"top-up {role}", "found")
                continue
            with transaction.atomic():
                profile = agents.lock_profile(AgentProfile.objects.get(user=agent))
                refusal = agents.usage_of(profile, currency).refusal(amount)
                if refusal is not None:
                    raise CommandError(
                        f"The review agent cannot top up {amount} {currency}: "
                        f"{refusal.code} (limit {refusal.limit}, remaining "
                        f"{refusal.remaining}). Raise the agent's caps in the console or "
                        f"the AGENT_DEFAULT_* settings.")
                tx = move_funds_atomic(None, wallet, amount, actor_id=agent.id,
                                       tx_type="topup", currency=currency,
                                       money_type=MoneyType.WALLET_MOVE,
                                       metadata={"agent_id": agent.id})
                agents.post_topup(profile, tx)
                agents.notify_client_on_commit(tx, client, profile)
            self._note(f"top-up {role}", f"{amount} {currency}")

    def _coaching(self):
        """`client`, `client2`, `client3` ask the trainer and the trainer accepts — the
        approval takes month 1 into escrow. `prospect` has asked and is waiting."""
        from notifications.domain.dispatcher import emit_event
        from notifications.domain.trainer_client_events import ClientRequestReceivedEvent
        from users.models import TrainerClientRelation
        from users.trainer_relations import quote_agreed_price
        from users.views import approve_trainer_relation

        trainer = self.users["trainer"]
        currency, money = self._money()
        if trainer.trainer_hourly_rate != money["fee"]:
            # The trainer's monthly fee, in their own currency, before anyone asks.
            trainer.trainer_hourly_rate = money["fee"]
            trainer.save(update_fields=["trainer_hourly_rate"])
        for role in (*COACHED, "prospect"):
            client = self.users[role]
            relation = TrainerClientRelation.objects.filter(trainer=trainer,
                                                            client=client).first()
            if relation is None:
                relation = TrainerClientRelation(
                    trainer=trainer, client=client, status="pending", initiated_by="client",
                    requested_at=timezone.now(),
                    client_note="أريد برنامجاً لبناء القوة، أتدرب أربعة أيام في الأسبوع.")
                quote_agreed_price(relation, trainer)
                relation.save()
                if role == "prospect":
                    transaction.on_commit(lambda c=client: emit_event(
                        ClientRequestReceivedEvent(actor_id=c.id, recipient_id=trainer.id,
                                                   client_name=c.full_name or c.username)))
                    self._note("request prospect", "sent")
                    continue
            elif role == "prospect" or relation.status == "approved":
                self._note(f"coaching {role}", "found")
                continue
            if relation.status != "pending":
                raise CommandError(f"The review {role}'s coaching is {relation.status}; "
                                   f"remove it in the console and run again.")
            refusal, relation = approve_trainer_relation(relation, trainer)
            if refusal is not None:
                raise CommandError(f"The trainer could not accept the review {role}: "
                                   f"{refusal.data.get('code')}")
            self._note(f"coaching {role}", "approved; month 1 held")

    # --------------------------------------------------------------- program
    def _template(self):
        from routine.models import Exercise, RoutineTemplate, RoutineTemplateExercise

        trainer = self.users["trainer"]
        template = RoutineTemplate.objects.filter(created_by=trainer,
                                                  name_en=TEMPLATE["name_en"]).first()
        if template is not None:
            return template
        exercises = {e.catalogue_key: e for e in Exercise.objects.filter(
            catalogue_key__in=[key for day in PROGRAM.values() for key, *_r in day])}
        with transaction.atomic():
            template = RoutineTemplate(created_by=trainer, days=len(PROGRAM), weeks=6,
                                       name=TEMPLATE["name_en"],
                                       description=TEMPLATE["description_en"], **TEMPLATE)
            template.save()
            for day, rows in PROGRAM.items():
                for order, (key, sets, reps, rest, _kg) in enumerate(rows, start=1):
                    RoutineTemplateExercise.objects.create(
                        template=template, exercise=exercises[key], sets=sets, reps=reps,
                        rest_time=rest, day=day, order=order)
        self._note("trainer template", "created")
        return template

    def _routines(self):
        """Each coached client's own copy of the trainer's template, assigned — which
        pays the trainer month 1 and the platform its commission."""
        from routine.assignment import assign_routine, routine_from_template
        from routine.models import Routine
        from routine.services import notify_assignment

        template = self._template()
        trainer = self.users["trainer"]
        today = timezone.localdate()
        for role in COACHED:
            client = self.users[role]
            if Routine.objects.filter(source_template=template, assigned_to=client).exists():
                self._note(f"routine {role}", "found")
                continue
            # `client` has trained it for two weeks (`_history`); the others started
            # this week.
            start = today - timedelta(days=14 if role == "client" else 3)
            routine = routine_from_template(template, created_by=trainer, days=len(PROGRAM),
                                            start_date=start, end_date=start + timedelta(weeks=6))
            Routine.objects.filter(pk=routine.pk).update(day_names=DAY_NAMES)
            assign_routine(routine, client, trainer)
            transaction.on_commit(lambda c=client, r=routine: notify_assignment(c, r))
            self._note(f"routine {role}", "assigned; month 1 paid out")

    def _premium(self):
        """The Premium entitlement, granted by the operator (`source=operator_grant`),
        the way the console's "activate" writes one: no payment row, because no money
        moved. Extended when fewer than GRANT_TOP_UP_DAYS remain."""
        from subscription.default_plans import PREMIUM_PLAN_NAME
        from subscription.models import Subscription, SubscriptionPlan

        client = self.users["client"]
        plan = SubscriptionPlan.objects.get(name_en=PREMIUM_PLAN_NAME)
        now = timezone.now()
        with transaction.atomic():
            subscription = (Subscription.objects.select_for_update(of=("self",))
                            .filter(user=client).first())
            if subscription is None:
                Subscription(user=client, plan=plan, status="active", source="operator_grant",
                             auto_renew=False, end_date=now + timedelta(days=GRANT_DAYS)).save()
                return self._note("premium grant", f"granted for {GRANT_DAYS} days")
            if subscription.is_active and subscription.end_date - now >= timedelta(
                    days=GRANT_TOP_UP_DAYS):
                return self._note("premium grant", "found")
            subscription.status, subscription.source = "active", "operator_grant"
            subscription.end_date = now + timedelta(days=GRANT_DAYS)
            subscription.save(update_fields=["status", "source", "end_date", "updated_at"])
            self._note("premium grant", f"extended to {GRANT_DAYS} days")

    def _history(self):
        """Two weeks of `client` training the routine four days a week, one day skipped,
        a little heavier every week — written as the app writes a logged workout."""
        from routine.models import ExerciseSetLog, Routine, UserExerciseProgress, WorkoutSession
        from routine.outcomes import record_day_quietly

        client = self.users["client"]
        routine = Routine.objects.filter(assigned_to=client,
                                         source_template__created_by=self.users["trainer"]
                                         ).order_by("id").first()
        if WorkoutSession.objects.filter(user=client, routine=routine).exists():
            return self._note("training history", "found")
        rnd = random.Random(7)
        today = timezone.localdate()
        days = [today - timedelta(days=back) for back in range(14, 0, -1)]
        days = [d for d in days if d.weekday() in TRAINING_WEEKDAYS]
        rows = {row.exercise.catalogue_key: row
                for row in routine.routine_exercises.select_related("exercise")}
        sessions = sets = 0
        with transaction.atomic():
            for index, day_date in enumerate(days):
                day = TRAINING_WEEKDAYS[day_date.weekday()]
                if index == 2:
                    record_day_quietly(client, routine, day, day_date, via="skipped")
                    continue
                start = timezone.make_aware(datetime.combine(day_date, time(18, 0)))
                session = WorkoutSession.objects.create(
                    user=client, routine=routine, day=day, start_time=start,
                    end_time=start + timedelta(minutes=48 + rnd.randint(0, 12)),
                    status="completed", effort_rating=rnd.choice([3, 4, 4, 5]))
                sessions += 1
                week = (today - day_date).days < 7
                for key, n_sets, reps, rest, kg in PROGRAM[day]:
                    row = rows[key]
                    weight = None if kg is None else kg + (2.5 if week else 0)
                    progress, _c = UserExerciseProgress.objects.get_or_create(
                        user=client, exercise=row.exercise, date=day_date,
                        defaults={"target_sets": n_sets})
                    logged = []
                    for number in range(1, n_sets + 1):
                        done = reps - (1 if number == n_sets and rnd.random() < 0.3 else 0)
                        logged.append(ExerciseSetLog.objects.create(
                            user_exercise_progress=progress, workout_session=session,
                            set_number=number, weight=weight, reps=done, rest_time=rest,
                            date=day_date))
                    # Received as the set was done, not today: the app syncs as the
                    # client trains, and a challenge counts only sets that arrived then.
                    ExerciseSetLog.objects.filter(pk__in=[s.pk for s in logged]).update(
                        created_at=start + timedelta(minutes=5))
                    sets += len(logged)
                    progress.completed_sets = n_sets
                    progress.total_repetitions = sum(s.reps for s in logged)
                    progress.total_weight = sum((s.weight or 0) * s.reps for s in logged)
                    progress.save(update_fields=["completed_sets", "total_repetitions",
                                                 "total_weight", "updated_at"])
                record_day_quietly(client, routine, day, day_date, via="session",
                                   session=session)
        self._note("training history", f"{sessions} sessions, {sets} sets")

    def _meal_plan(self):
        from diet.models import DietPlan
        from diet.services.plan_generation import generate_and_save

        client = self.users["client"]
        today = timezone.localdate()
        if DietPlan.objects.filter(user=client, is_active=True, is_draft=False,
                                   start_date__lte=today, end_date__gte=today).exists():
            return self._note("meal plan", "found")
        _output, plan = generate_and_save(client, meal_count=3, snack_count=1,
                                          duration_days=7)
        self._note("meal plan", f"7 days from {plan.start_date}")

    def _challenge(self):
        """A running challenge the trainer set, joined by the coached clients through
        the join's own steps: participation, count, recount, ranking."""
        from social import standing
        from social.challenge_progress import recount
        from social.models import Challenge, ChallengeParticipation

        trainer = self.users["trainer"]
        now = timezone.now()
        challenge = (Challenge.objects.filter(creator=trainer, title_en=CHALLENGE["title_en"])
                     .order_by("-id").first())
        if challenge is None or not challenge.is_active:
            challenge = Challenge(
                creator=trainer, title=CHALLENGE["title_en"], description=CHALLENGE["description_en"],
                rules=CHALLENGE_RULES, challenge_type="workout", metric="sets",
                start_date=now - timedelta(days=7), end_date=now + timedelta(days=7),
                status="active", target_value=100, unit="sets",
                reward_description="شارة القوة", **CHALLENGE)
            challenge.save()
            self._note("challenge", "created")
        else:
            self._note("challenge", "found")
        joined = []
        with transaction.atomic():
            locked = Challenge.objects.select_for_update().get(pk=challenge.pk)
            for role in COACHED:
                _row, created = ChallengeParticipation.objects.get_or_create(
                    user=self.users[role], challenge=locked)
                if created:
                    joined.append(self.users[role].id)
            if joined:
                Challenge.objects.filter(pk=locked.pk).update(
                    participants_count=ChallengeParticipation.objects.filter(
                        challenge=locked).count())
        if joined:
            recount(challenge, joined)
            standing.write_places(challenge)
            self._note("challenge joined", f"{len(joined)} participant(s)")


def _placeholder_document(slot: str, index: int) -> bytes:
    """A PNG that says what it is. Distinct per slot (the service refuses the same file
    in two slots) and large enough to pass the readability floor."""
    from PIL import Image, ImageDraw

    colours = [(236, 240, 244), (244, 236, 236), (236, 244, 238), (244, 242, 232),
               (238, 236, 244)]
    image = Image.new("RGB", (900, 560), colours[index % len(colours)])
    draw = ImageDraw.Draw(image)
    lines = ["YALLA GYM - STORE REVIEW DEMO ACCOUNT", "", "NOT AN IDENTITY DOCUMENT",
             "Placeholder for: " + slot, "No real person's details are on file."]
    for number, line in enumerate(lines):
        draw.text((60, 120 + 50 * number), line, fill=(40, 40, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
