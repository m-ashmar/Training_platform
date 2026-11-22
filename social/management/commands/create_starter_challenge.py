"""Open the platform's starter challenge: a challenge to join on day one (Road to 10 S3.4).

    python manage.py create_starter_challenge --creator <staff username or email>
    python manage.py create_starter_challenge --creator ops --days 30 --target 12
    python manage.py create_starter_challenge --creator ops --force

The live server opened with no running challenge, so the Community tab's first card had
nothing to offer. This writes one for the whole community: `training_days` (days with
counted training, from the sets people log: nothing to type), `--target` days in
`--days` days, starting now, titled and described in Arabic and English. Its creator is
an operator (an active STAFF account, named by `--creator`), so it is ranked (F20: staff
or a verified trainer) and the Challenge Winner badge can be earned in it.

A command and not the console's add form, because the form asks the operator for every
field of a challenge with nothing to say what a good first one is, and this is run at
launch beside `bootstrap_platform` (DEPLOY_CHECKLIST.md section 6). The console still
edits, hides and closes it like any other.

Safe to run again: while a starter is upcoming or running (a community challenge made by
staff, not hidden, not over) it creates nothing and says which one is open. `--force`
opens another all the same. It does not go through the API, so the hosting limit
(`CHALLENGE_HOST_LIMIT`) does not apply, as it does not to staff there.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone


def _days_ar(n: int) -> str:
    """«N يوم» with the Arabic number agreement: 1 and 2 have their own words, 3 to 10
    take the plural, 11 and up the singular accusative."""
    if n == 1:
        return "يوم واحد"
    if n == 2:
        return "يومان"
    if 3 <= n <= 10:
        return f"{n} أيام"
    return f"{n} يوماً"


def starter_text(target: int, days: int) -> dict:
    """The starter's title and description in both languages."""
    return {
        "title_en": f"Train {target} days in {days}",
        "title_ar": f"تمرّن {_days_ar(target)} خلال {_days_ar(days)}",
        "description_en": (
            f"The Yalla Gym starter challenge. Train on {target} different days within "
            f"{days} days to finish it. Every workout you log counts on its own: there "
            f"is nothing to type."),
        "description_ar": (
            f"تحدي البداية من يلا جيم. تمرّن في {_days_ar(target)} مختلفة خلال "
            f"{_days_ar(days)} لتُكمله. كل تمرين تسجّله يُحتسب تلقائياً، "
            f"ولا حاجة لكتابة أي شيء."),
    }


class Command(BaseCommand):
    help = ("Open the platform's starter challenge for the whole community (Road to 10 "
            "S3.4). Creates nothing while one is upcoming or running, unless --force.")

    def add_arguments(self, parser):
        parser.add_argument("--creator", required=True,
                            help="Username or email of an active staff account.")
        parser.add_argument("--days", type=int, default=30,
                            help="How long it runs, from now (default 30).")
        parser.add_argument("--target", type=int, default=12,
                            help="Training days to finish it (default 12).")
        parser.add_argument("--force", action="store_true",
                            help="Open one even if a starter is already open.")

    def handle(self, *args, **options):
        from social.challenge_cache import bump_everyone
        from social.models import Challenge
        from users.models import CustomUser

        days, target = options["days"], options["target"]
        if days < 1:
            raise CommandError("--days must be at least 1.")
        if not 1 <= target <= days:
            raise CommandError("--target must be between 1 and --days.")
        who = options["creator"].strip()
        creator = (CustomUser.objects
                   .filter(Q(username=who) | Q(email__iexact=who), is_active=True)
                   .first())
        if creator is None:
            raise CommandError(f"No active account is named {who!r}.")
        if not creator.is_staff:
            raise CommandError(
                f"{who!r} is not staff. The starter is the platform's: its creator is an "
                "operator, which is what ranks it.")

        now = timezone.now()
        with transaction.atomic():
            open_one = (Challenge.objects
                        .filter(creator__is_staff=True, audience=Challenge.AUDIENCE_COMMUNITY,
                                is_hidden=False, end_date__gte=now)
                        .exclude(status__in=Challenge.TERMINAL_STATUSES)
                        .order_by("-created_at", "-id").first())
            if open_one is not None and not options["force"]:
                self.stdout.write(
                    f"A starter challenge is already open: #{open_one.pk} "
                    f"{open_one.title!r}, until {open_one.end_date:%Y-%m-%d}. "
                    "Created 0 (--force opens another).")
                return
            text = starter_text(target, days)
            challenge = Challenge.objects.create(
                title=text["title_en"], description=text["description_en"],
                title_en=text["title_en"], title_ar=text["title_ar"],
                description_en=text["description_en"],
                description_ar=text["description_ar"],
                challenge_type="workout", metric="training_days",
                target_value=float(target), unit="days",
                start_date=now, end_date=now + timedelta(days=days),
                audience=Challenge.AUDIENCE_COMMUNITY, creator=creator)
        # Every member's cached challenge list (120 s) shows it on the next read.
        bump_everyone()
        self.stdout.write(self.style.SUCCESS(
            f"Created 1: challenge #{challenge.pk} {text['title_ar']!r} "
            f"({text['title_en']!r}), running until {challenge.end_date:%Y-%m-%d}."))
