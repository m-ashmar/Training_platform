"""Reporting, blocking and AI consent.

Three store-review blockers live here. A build that carries content other people wrote
has to let a person report it and block its author, and a build whose AI reads a
person's training, food and body data has to ask first. None of it depends on who is
asking, where they are or what date it is — the rules below are the same for every
account on every build (`CLAUDE.md`, rule zero).

The three models are deliberately independent of the apps they protect:

* `Report` names its target by TYPE AND ID, never by a foreign key. Reportable things
  live in seven apps and one of them (`chat_message`) is deleted by the person's own
  GDPR request; a hard FK would make every one of those deletions a moderation
  problem — either it cascades the complaint away or it PROTECTs and the delete fails.
  A dangling `target_id` is the correct outcome: the report stays, and the console
  shows the target as gone (`moderation/targets.py`).
* `Block` is owned by the blocker and symmetric in EFFECT: one row hides each person
  from the other (`moderation/services.py` `blocked_ids`). Only the blocker can undo it.
* `AIConsent` is one row per person, holding the version of the policy they agreed to,
  so a later version can ask again without losing the record of what was agreed before.
"""
from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils.translation import pgettext_lazy

#: Short labels like "User", "Open" or "Report" are words the rest of the catalogue
#: will want for other things, and a bare msgid would hand one Arabic word to all of
#: them. Every label below is declared in the "moderation" context so it can be
#: translated for THIS screen and nowhere else.
def _label(text):
    return pgettext_lazy("moderation", text)


class TargetType(models.TextChoices):
    """What can be reported. The app sends one of these strings verbatim.

    `chat_message` is a message in a conversation with the AI coach (ai_assistant) and
    stays that: a shipped value never changes meaning. A message in the trainer–client
    chat (`chat.Message`) is `coach_message` (SIDE_BUGS S-105). `challenge` is a
    community challenge — its title, description, rules, reward and image are written
    by whoever created it and shown to everyone (S-065).
    """

    USER = "user", _label("User")
    CHAT_MESSAGE = "chat_message", _label("Chat message")
    ROUTINE = "routine", _label("Routine")
    DIET_PLAN = "diet_plan", _label("Diet plan")
    EXERCISE = "exercise", _label("Exercise")
    SOCIAL_POST = "social_post", _label("Post")
    COMMENT = "comment", _label("Comment")
    COACH_MESSAGE = "coach_message", _label("Trainer chat message")
    CHALLENGE = "challenge", _label("Challenge")


class ReportReason(models.TextChoices):
    """A closed set: the operator queue filters on it and the app shows a fixed list.

    Free text goes in `note`, never here — a reason the app invents is refused, so the
    queue can be counted and triaged by category.
    """

    SPAM = "spam", _label("Spam")
    HARASSMENT = "harassment", _label("Harassment or bullying")
    NUDITY = "nudity", _label("Nudity or sexual content")
    VIOLENCE = "violence", _label("Violence or threats")
    SCAM = "scam", _label("Scam or fraud")
    IMPERSONATION = "impersonation", _label("Impersonation")
    OTHER = "other", _label("Something else")


class ReportStatus(models.TextChoices):
    OPEN = "open", _label("Open")
    REVIEWING = "reviewing", _label("Being reviewed")
    ACTIONED = "actioned", _label("Action taken")
    DISMISSED = "dismissed", _label("Dismissed")


#: A report still on the operator's desk. The partial unique index below is scoped to
#: these two, so one person cannot file the same complaint twice while it is waiting —
#: and CAN report the same target again after it was handled, because the second
#: offence is a new complaint, not a duplicate of the first.
UNRESOLVED_STATUSES = (ReportStatus.OPEN, ReportStatus.REVIEWING)

#: How much the reporter may write. A hard column width, not a validator: the value is
#: shown to an operator and stored forever, and an unbounded TEXT column reachable by
#: any signed-in account is a free database-filling endpoint.
NOTE_MAX_LENGTH = 1000
RESOLUTION_NOTE_MAX_LENGTH = 1000


class Report(models.Model):
    """One complaint about one thing, and what the platform did about it."""

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reports_made",
        help_text="Who complained.")
    target_type = models.CharField(max_length=16, choices=TargetType.choices)
    #: The target's primary key in its own app's table. NOT a foreign key, and not a
    #: GenericForeignKey either: a GFK is still a real pointer through
    #: `django_contenttypes` and brings the same deletion coupling with an extra join.
    target_id = models.PositiveBigIntegerField()
    reason = models.CharField(max_length=16, choices=ReportReason.choices)
    #: The reporter's own words. Optional: the reason code is what the queue triages on.
    note = models.CharField(max_length=NOTE_MAX_LENGTH, blank=True, default="")

    status = models.CharField(max_length=10, choices=ReportStatus.choices,
                              default=ReportStatus.OPEN)
    #: The operator who closed it. SET_NULL, not CASCADE: an operator leaving the
    #: company must not delete the record of every complaint they ever handled.
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reports_handled")
    handled_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=RESOLUTION_NOTE_MAX_LENGTH,
                                       blank=True, default="",
                                       help_text="What the operator decided, and why.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _label("Report")
        verbose_name_plural = _label("Reports")
        # `id` tiebreaker: `created_at` alone is not a total order, so LIMIT/OFFSET
        # paging over a queue that takes several reports a second repeats and hides rows.
        ordering = ["-created_at", "-id"]
        indexes = [
            # The queue read: WHERE status = 'open' ORDER BY created_at.
            models.Index(fields=["status", "created_at"], name="report_queue_idx"),
            # "Everything filed against this thing", which is how an operator decides
            # whether one complaint is a grudge or the tenth report of the same post.
            models.Index(fields=["target_type", "target_id"], name="report_target_idx"),
        ]
        constraints = [
            # One open complaint per person per target. A partial unique index, the
            # same shape `users.TrainerClientRelation` uses for its pending request:
            # the uniqueness has to stop applying once the row is resolved, and a
            # plain unique_together would silence every later report forever.
            models.UniqueConstraint(
                fields=["reporter", "target_type", "target_id"],
                condition=Q(status__in=[s.value for s in UNRESOLVED_STATUSES]),
                name="one_open_report_per_target_per_reporter",
            ),
        ]

    def __str__(self):
        return f"{self.reporter_id} reported {self.target_type} {self.target_id} ({self.status})"

    @property
    def is_open(self) -> bool:
        return self.status in {s.value for s in UNRESOLVED_STATUSES}


class Block(models.Model):
    """One person has hidden another. Owned by the blocker, felt by both."""

    blocker = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="blocks_made")
    blocked = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="blocks_received")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _label("Block")
        verbose_name_plural = _label("Blocks")
        ordering = ["-created_at", "-id"]
        indexes = [
            # `blocked_ids` reads both directions in one query; the unique index below
            # serves `blocker`, this one serves the other side of the OR.
            models.Index(fields=["blocked"], name="block_reverse_idx"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["blocker", "blocked"],
                                    name="one_block_per_pair"),
            # Blocking yourself would erase you from your own feed: `blocked_ids`
            # returns both sides of every row, and every list filters on it.
            models.CheckConstraint(condition=~Q(blocker=F("blocked")),
                                   name="cannot_block_yourself"),
        ]

    def __str__(self):
        return f"{self.blocker_id} blocked {self.blocked_id}"


class AIConsent(models.Model):
    """Whether this person has agreed to the AI coach reading their data, and to which
    policy version.

    One row per person, rewritten in place. `granted_at` and `revoked_at` are kept
    rather than a single boolean because "never asked" and "asked and said no" are
    different states: the app shows the consent sheet for the first and a setting the
    person can switch back on for the second.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_consent")
    granted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    #: The version of the AI data policy the person agreed to. A new version is a new
    #: question: the app compares this with the version it is showing and asks again.
    policy_version = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _label("AI consent")
        verbose_name_plural = _label("AI consent")
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return f"{self.user_id}: {'granted' if self.is_active else 'not granted'}"

    @property
    def is_active(self) -> bool:
        """Granted, and not revoked since.

        Compared rather than "revoked_at is None" so that re-granting after a
        revocation is one write to `granted_at` and cannot leave a row that reads as
        both granted and revoked.
        """
        if self.granted_at is None:
            return False
        return self.revoked_at is None or self.revoked_at <= self.granted_at


class BannedTerm(models.Model):
    """One word or phrase nobody may write where other people read it (spec B1).

    Edited by operators in the console (`/dj-admin/moderation/bannedterm/`); matched by
    `moderation/ugc_filter.py`, whose docstring says what is checked, how a term is
    matched and how the list is cached. Every save and delete moves the cache's version,
    so the next write anywhere obeys the change.

    Console-only: nothing here reaches the app (a refusal never names the term), so the
    labels are plain English like every other console page.
    """

    class Language(models.TextChoices):
        ARABIC = "ar", "Arabic"
        ENGLISH = "en", "English"
        ANY = "any", "Any / mixed"

    class Category(models.TextChoices):
        OBSCENITY = "obscenity", "Obscenity or sexual"
        INSULT = "insult", "Insult"
        HATE = "hate", "Slur or hate"
        SPAM = "spam", "Spam or contact-for-sale"
        OTHER = "other", "Other"

    #: As the operator typed it. Matching compares its folded form (`normalized`).
    term = models.CharField(max_length=100)
    #: `term` folded the way text is folded before matching (`ugc_filter.normalise`).
    #: Unique, so the same word cannot be listed twice under two spellings.
    normalized = models.CharField(max_length=200, unique=True, editable=False)
    whole_word = models.BooleanField(
        default=True,
        help_text="On: the term must stand as its own word (\"cunt\" does not match "
                  "\"Scunthorpe\"). Off: it matches inside other words too (\"fuck\" "
                  "matches \"motherfucker\"): only for stems no innocent word "
                  "contains.")
    language = models.CharField(max_length=3, choices=Language.choices,
                                default=Language.ANY,
                                help_text="For the operator's own sorting. Every term "
                                          "is matched in every text.")
    category = models.CharField(max_length=10, choices=Category.choices,
                                default=Category.OTHER)
    notes = models.TextField(blank=True, default="",
                             help_text="Why it is listed, where it was seen.")
    is_active = models.BooleanField(default=True,
                                    help_text="Off: kept in the list, matched nowhere.")
    #: The seeded starter list is written with this set: the owner reviews every seeded
    #: term (keep, change, switch off) before launch. An operator's own row is False.
    needs_review = models.BooleanField(
        default=False, help_text="Seeded, and not yet reviewed by the owner.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Banned term"
        verbose_name_plural = "Banned terms"
        ordering = ["normalized", "id"]

    def __str__(self):
        return self.term

    def clean(self):
        from django.core.exceptions import ValidationError

        from .ugc_filter import MIN_TERM_LETTERS, letter_count, normalise

        folded = normalise(self.term)
        if letter_count(folded) < MIN_TERM_LETTERS:
            raise ValidationError({"term": f"A term needs at least {MIN_TERM_LETTERS} "
                                           "letters: a single letter matches everywhere."})
        if len(folded) > self._meta.get_field("normalized").max_length:
            raise ValidationError({"term": "This term is too long once folded."})
        taken = BannedTerm.objects.filter(normalized=folded).exclude(pk=self.pk).first()
        if taken is not None:
            raise ValidationError({"term": f"Already listed as \"{taken.term}\" "
                                           "(the same word once folded)."})

    def save(self, *args, **kwargs):
        from .ugc_filter import normalise

        self.term = (self.term or "").strip()
        self.normalized = normalise(self.term)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "term" in update_fields:
            kwargs["update_fields"] = {*update_fields, "normalized"}
        super().save(*args, **kwargs)


def _banned_terms_changed(**_kwargs):
    """Every process drops its compiled list: now (this process, and any that reads
    before the commit sees the old rows) and again once the change is committed."""
    from django.db import transaction

    from . import ugc_filter

    ugc_filter.invalidate()
    transaction.on_commit(ugc_filter.invalidate)


models.signals.post_save.connect(_banned_terms_changed, sender=BannedTerm,
                                 dispatch_uid="moderation.banned_terms.saved")
models.signals.post_delete.connect(_banned_terms_changed, sender=BannedTerm,
                                   dispatch_uid="moderation.banned_terms.deleted")
