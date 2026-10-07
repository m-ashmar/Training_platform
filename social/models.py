import uuid
import os
"""
Social Features Models for Training Platform

This module provides social networking capabilities including user following,
content sharing, community interactions, and social feeds.
"""

from django.db import models
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext_lazy
from users.models import CustomUser
from training_platform.model_validation import RowValidationMixin


def _random_media_path(folder, filename):
    """Return `<folder>/<random>.<ext>`.

    A static `upload_to` keeps the uploader's own filename, so the stored path is
    `posts/<their filename>` — directly guessable. Media is served without any
    authorization check, so a private post's image was retrievable by anyone who
    guessed the name. A random token makes the URL itself the capability.
    """
    ext = os.path.splitext(filename)[1].lower()[:10] or '.bin'
    return os.path.join(folder, f"{uuid.uuid4().hex}{ext}")


def post_image_upload_path(instance, filename):
    return _random_media_path('posts', filename)


def challenge_image_upload_path(instance, filename):
    return _random_media_path('challenges', filename)


def achievement_icon_upload_path(instance, filename):
    return _random_media_path('achievements', filename)


class UserFollow(RowValidationMixin, models.Model):
    """
    Track user following relationships
    """
    follower = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='following',
        db_index=True
    )
    following = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='followers',
        db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    # Follow preferences
    notify_activity = models.BooleanField(default=True)
    show_in_feed = models.BooleanField(default=True)
    
    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']
        unique_together = ['follower', 'following']
        indexes = [
            models.Index(fields=['follower', 'created_at']),
            models.Index(fields=['following', 'created_at']),
        ]
        verbose_name = "User Follow"
        verbose_name_plural = "User Follows"
    
    def __str__(self):
        return f"{self.follower.username} follows {self.following.username}"
    
    def clean(self):
        """Prevent users from following themselves"""
        from django.core.exceptions import ValidationError
        from django.utils.translation import gettext_lazy as _
        if self.follower == self.following:
            raise ValidationError(_("Users cannot follow themselves"), code="self_follow")

    def save(self, *args, **kwargs):
        self.validate_row()
        super().save(*args, **kwargs)


class Post(models.Model):
    """
    User posts for social feed
    """
    POST_TYPES = [
        ('text', 'Text Post'),
        ('workout', 'Workout Share'),
        ('achievement', 'Achievement'),
        ('progress', 'Progress Update'),
        ('meal', 'Meal Share'),
        ('motivation', 'Motivation'),
        ('question', 'Question'),
        ('tip', 'Fitness Tip'),
    ]
    
    VISIBILITY_CHOICES = [
        ('public', 'Public'),
        ('followers', 'Followers Only'),
        ('private', 'Private'),
    ]
    
    author = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='posts',
        db_index=True
    )
    post_type = models.CharField(
        max_length=20,
        choices=POST_TYPES,
        default='text',
        db_index=True
    )
    title = models.CharField(max_length=200, blank=True)
    content = models.TextField()
    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default='public',
        db_index=True
    )
    
    # Media attachments
    image = models.ImageField(
        upload_to=post_image_upload_path,
        blank=True,
        null=True
    )
    
    # Related content (generic foreign key)
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    object_id = models.PositiveIntegerField(null=True, blank=True)
    related_object = GenericForeignKey('content_type', 'object_id')
    
    # Engagement metrics
    likes_count = models.PositiveIntegerField(default=0)
    comments_count = models.PositiveIntegerField(default=0)
    shares_count = models.PositiveIntegerField(default=0)
    views_count = models.PositiveIntegerField(default=0)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Moderation
    is_flagged = models.BooleanField(default=False)
    is_hidden = models.BooleanField(default=False)
    
    class Meta:
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['author', 'visibility', 'created_at']),
            models.Index(fields=['post_type', 'visibility', 'created_at']),
            models.Index(fields=['visibility', 'created_at']),
            # Matches the real access pattern: WHERE <owner>=? ORDER BY created_at DESC, id DESC.
            # A single-column created_at index cannot serve that; this one can.
            models.Index(fields=['author', '-created_at', '-id'], name='post_owner_recent_idx'),
        ]
        verbose_name = "Post"
        verbose_name_plural = "Posts"
    
    def __str__(self):
        return f"{self.author.username} - {self.get_post_type_display()}"
    
    def can_be_viewed_by(self, user):
        """Check if user can view this post"""
        if self.is_hidden:
            return self.author == user
        
        if self.visibility == 'public':
            return True
        elif self.visibility == 'private':
            return self.author == user
        elif self.visibility == 'followers':
            if self.author == user:
                return True
            return UserFollow.objects.filter(
                follower=user,
                following=self.author
            ).exists()
        
        return False


class PostLike(models.Model):
    """
    Track post likes
    """
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='post_likes',
        db_index=True
    )
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name='likes',
        db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']
        unique_together = ['user', 'post']
        indexes = [
            models.Index(fields=['post', 'created_at']),
            models.Index(fields=['user', 'created_at']),
        ]
        verbose_name = "Post Like"
        verbose_name_plural = "Post Likes"
    
    def __str__(self):
        return f"{self.user.username} likes post by {self.post.author.username}"


class Comment(models.Model):
    """
    Comments on posts
    """
    author = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='comments',
        db_index=True
    )
    post = models.ForeignKey(
        Post,
        on_delete=models.CASCADE,
        related_name='comments',
        db_index=True
    )
    content = models.TextField()
    
    # Reply to another comment (nested comments)
    parent_comment = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='replies'
    )
    
    # Engagement
    likes_count = models.PositiveIntegerField(default=0)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Moderation
    is_flagged = models.BooleanField(default=False)
    is_hidden = models.BooleanField(default=False)
    
    class Meta:
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['post', 'created_at']),
            models.Index(fields=['author', 'created_at']),
            models.Index(fields=['parent_comment', 'created_at']),
            # Matches the real access pattern: WHERE <owner>=? ORDER BY created_at DESC, id DESC.
            # A single-column created_at index cannot serve that; this one can.
            models.Index(fields=['author', '-created_at', '-id'], name='comment_owner_recent_idx'),
        ]
        verbose_name = "Comment"
        verbose_name_plural = "Comments"
    
    def __str__(self):
        return f"Comment by {self.author.username} on {self.post.id}"


class CommentLike(models.Model):
    """
    Track comment likes
    """
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='comment_likes',
        db_index=True
    )
    comment = models.ForeignKey(
        Comment,
        on_delete=models.CASCADE,
        related_name='likes',
        db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']
        unique_together = ['user', 'comment']
        verbose_name = "Comment Like"
        verbose_name_plural = "Comment Likes"
    
    def __str__(self):
        return f"{self.user.username} likes comment by {self.comment.author.username}"


class Challenge(models.Model):
    """
    Community challenges
    """
    CHALLENGE_TYPES = [
        ('workout', 'Workout Challenge'),
        ('diet', 'Diet Challenge'),
        ('weight_loss', 'Weight Loss Challenge'),
        ('endurance', 'Endurance Challenge'),
        ('strength', 'Strength Challenge'),
        ('habit', 'Habit Challenge'),
        ('custom', 'Custom Challenge'),
    ]
    
    STATUS_CHOICES = [
        ('upcoming', 'Upcoming'),
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    title = models.CharField(max_length=200)
    description = models.TextField()
    challenge_type = models.CharField(
        max_length=20,
        choices=CHALLENGE_TYPES,
        db_index=True
    )
    
    # Challenge details
    creator = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='created_challenges',
        db_index=True
    )
    start_date = models.DateTimeField(db_index=True)
    end_date = models.DateTimeField(db_index=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='upcoming',
        db_index=True
    )
    
    # Challenge parameters
    target_value = models.FloatField(null=True, blank=True)
    unit = models.CharField(max_length=20, blank=True)
    rules = models.TextField(blank=True)

    #: What the challenge measures, and so whether the server counts it. `manual` (the
    #: default, and every challenge written before this column): the participant logs
    #: their number through `update_progress`. Anything else is counted from the
    #: training they log (`social/challenge_progress.py`), and `update_progress` refuses
    #: it (`progress_auto_counted`). The 09-community design counts progress
    #: automatically wherever it can be measured (BACKEND_BUGS B-075); `challenge_type`
    #: and `unit` are free descriptions and cannot say which number that is.
    #:
    #: The owner's decision of 2026-09-28 (BACKEND_BUGS B-161) added two, for the
    #: CreateChallenge artboard: `exercise_reps` (the reps of ONE exercise, named in
    #: `exercise`, which it requires) and `streak_days` (the CURRENT run of consecutive
    #: days with a counted set). «عدد التمارين» is `workouts` (workouts finished).
    METRIC_MANUAL = 'manual'
    METRIC_EXERCISE_REPS = 'exercise_reps'
    METRIC_STREAK_DAYS = 'streak_days'
    METRIC_CHOICES = [
        (METRIC_MANUAL, gettext_lazy('Logged by hand')),
        ('volume_kg', gettext_lazy('Weight lifted (kg)')),
        ('sets', gettext_lazy('Sets logged')),
        ('workouts', gettext_lazy('Workouts finished')),
        ('training_days', gettext_lazy('Days trained')),
        (METRIC_EXERCISE_REPS, gettext_lazy('Reps of one exercise')),
        (METRIC_STREAK_DAYS, gettext_lazy('Days in a row')),
    ]
    metric = models.CharField(max_length=20, choices=METRIC_CHOICES, default=METRIC_MANUAL)

    #: The exercise whose reps an `exercise_reps` challenge counts (B-161); null for
    #: every other metric. A catalogue exercise or one of the creator's own. SET_NULL:
    #: an exercise deleted later leaves the challenge, its standing frozen as it stood
    #: (`challenge_progress.recount` does not recount a challenge that lost it).
    exercise = models.ForeignKey('routine.Exercise', on_delete=models.SET_NULL,
                                 null=True, blank=True, related_name='challenges')

    # Participation
    max_participants = models.PositiveIntegerField(null=True, blank=True)
    participants_count = models.PositiveIntegerField(default=0)
    
    # Rewards
    reward_description = models.TextField(blank=True)
    
    # Media
    image = models.ImageField(
        upload_to=challenge_image_upload_path,
        blank=True,
        null=True
    )

    #: Hidden by an operator (the console's hide action), after a report or on sight.
    #: A challenge is written by whoever created it and shown to everyone, so it needs
    #: the same moderation switch a post and a comment have (SIDE_BUGS S-065). A hidden
    #: challenge is served to nobody, its creator included.
    is_hidden = models.BooleanField(default=False)

    #: When the winner of this challenge was SETTLED (owner, 2026-09-24, F20): the
    #: challenge ended at least `REVIEW_HOURS` ago, the standing was re-ranked one last
    #: time, and whoever holds place 1 was checked for the Challenge Winner badge — or
    #: nobody could be (a manual or unranked challenge, nobody at the target). Written
    #: ONCE (`social/tasks.py settle_winners`); null while the standing is still open to
    #: review, and while the person at place 1 is flagged and no operator has decided.
    winner_checked_at = models.DateTimeField(null=True, blank=True)

    #: How many ACTIVE participants (`social/standing.py active_participants`) the
    #: challenge had when it was settled — the settlement's verdict on the owner's
    #: minimum (2026-09-24, `MIN_PARTICIPANTS_FOR_WINNER`), written in the same UPDATE as
    #: `winner_checked_at`, under the challenge lock. The badge engine reads THIS
    #: (`achievements/engine.py challenge_wins`), never a recount: an erased rival's
    #: participation row is deleted, so a recount after the settlement would take back a
    #: win that was valid when it was settled. Null until settled.
    settled_active_participants = models.PositiveIntegerField(null=True, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    #: How long after its end a challenge's standing stays open for review before the
    #: winner is final (F20). An operator reviews flagged participants in that window.
    #: Equal to `social/challenge_progress.py LATE_HOURS`: a set of the window's last day
    #: that syncs late (a phone offline) is still counted up to LATE_HOURS after its day,
    #: so the winner must not settle before those sets can arrive
    #: (tests/test_owner_decisions_0924_challenges.py pins REVIEW_HOURS >= LATE_HOURS).
    REVIEW_HOURS = 72

    class Meta:
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['status', 'start_date']),
            models.Index(fields=['challenge_type', 'status']),
            models.Index(fields=['creator', 'status']),
            # Matches the real access pattern: WHERE <owner>=? ORDER BY created_at DESC, id DESC.
            # A single-column created_at index cannot serve that; this one can.
            models.Index(fields=['creator', '-created_at', '-id'], name='challenge_owner_recent_idx'),
        ]
        verbose_name = "Challenge"
        verbose_name_plural = "Challenges"
    
    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"
    
    #: Statuses a person sets. Everything else follows from the dates.
    TERMINAL_STATUSES = ('completed', 'cancelled')

    @property
    def lifecycle_status(self):
        """Where the challenge is, from its dates unless someone closed it.

        `status` defaults to 'upcoming' and nothing ever moved it to 'active' — no task,
        no signal — while `is_active` required 'active'. Every challenge created through
        the API therefore stayed "not running" for its whole window, and joining it was
        refused until its creator happened to PATCH the status by hand. Only closing a
        challenge (completed / cancelled) is a decision; upcoming vs running vs over is
        a fact about the clock.
        """
        if self.status in self.TERMINAL_STATUSES:
            return self.status
        now = timezone.now()
        if now < self.start_date:
            return 'upcoming'
        if now > self.end_date:
            return 'completed'
        return 'active'

    @property
    def is_active(self):
        """Is the challenge running right now?"""
        return self.lifecycle_status == 'active'

    def counting_open(self, now=None) -> bool:
        """May a recount still move this challenge's numbers?

        While it runs, and after its end for `LATE_HOURS` more — the sets of its window
        that sync late still count (`challenge_progress.credible_sets` refuses any set
        received later than that) — until its winner is settled. Never for a manual,
        hidden or cancelled challenge, or one not started yet."""
        from .challenge_progress import LATE_HOURS

        now = now or timezone.now()
        return (self.auto_counted and not self.is_hidden and self.status != 'cancelled'
                and self.winner_checked_at is None and self.start_date <= now
                and now <= self.end_date + timedelta(hours=LATE_HOURS))

    @property
    def auto_counted(self):
        """Does the server count this challenge's progress from logged training?"""
        return self.metric != self.METRIC_MANUAL

    #: Creators whose auto-counted challenges are RANKED: a VERIFIED trainer, or staff.
    RANKED_CREATOR_TYPES = ('trainer',)

    @property
    def ranked(self):
        """Can a first place in this challenge earn anything (owner, 2026-09-24, F20)?

        Only when the server counts it (`auto_counted`: no self-reported number wins a
        prize) AND staff or a VERIFIED trainer (`trainer_is_verified`, set by an
        operator) created it. A client cannot open a challenge of their own and crown
        themselves, and since anyone may sign up as a trainer, neither can a throwaway
        trainer account made to crown a friend. The Challenge Winner badge, and anything else
        a first place earns, is awarded only for a ranked challenge. `ranked_q` is the
        same rule as a query.
        """
        creator = self.creator
        return bool(self.auto_counted and creator is not None and (
            creator.is_staff or (creator.user_type in self.RANKED_CREATOR_TYPES
                                 and creator.trainer_is_verified)))

    @classmethod
    def ranked_q(cls, prefix=''):
        """`ranked` as a query, over `<prefix>` (e.g. `'challenge__'`)."""
        return (~models.Q(**{f'{prefix}metric': cls.METRIC_MANUAL})
                & (models.Q(**{f'{prefix}creator__is_staff': True})
                   | models.Q(**{f'{prefix}creator__user_type__in': cls.RANKED_CREATOR_TYPES,
                                 f'{prefix}creator__trainer_is_verified': True})))

    @property
    def review_ends_at(self):
        """When the review of the standing closes: `REVIEW_HOURS` after the end."""
        from datetime import timedelta

        return self.end_date + timedelta(hours=self.REVIEW_HOURS)

    @property
    def winner_final(self):
        """The challenge ended at least `REVIEW_HOURS` ago and its winner is settled."""
        return self.winner_checked_at is not None

    @classmethod
    def advance_stored_phases(cls, now=None):
        """Bring the stored `status` column in line with the clock.

        `lifecycle_status` answers the API, but the column is what every operator
        surface reads: admin_dashboard's changelist displays it and filters on it, so a
        challenge the app called 'active' still listed as 'upcoming' and filtering
        status=active found nothing. Two UPDATEs, hourly — a close (completed/cancelled)
        set by a person is never overwritten.

        Returns (started, ended).
        """
        now = now or timezone.now()
        started = cls.objects.filter(status='upcoming', start_date__lte=now,
                                     end_date__gte=now).update(status='active')
        ended = cls.objects.filter(status__in=('upcoming', 'active'),
                                   end_date__lt=now).update(status='completed')
        return started, ended

    @classmethod
    def running_q(cls, now=None):
        """`is_active` as a query."""
        now = now or timezone.now()
        return (~models.Q(status__in=cls.TERMINAL_STATUSES)
                & models.Q(start_date__lte=now, end_date__gte=now))


class ChallengeParticipation(models.Model):
    """
    Track user participation in challenges
    """
    STATUS_CHOICES = [
        ('joined', 'Joined'),
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('dropped', 'Dropped Out'),
    ]

    #: Participations that hold NO place in the standing. Someone who dropped out is
    #: behind nobody and ahead of nobody: the leaderboard does not list them, the home
    #: card's "how many are ahead of me" does not count them, and the stored `rank` is
    #: NULL for them — otherwise a rival who quit while leading kept rank 1, and with it
    #: the Challenge Winner badge's `rank=1`, from the person actually in front
    #: (SIDE_BUGS S-045). One tuple, read by all three, so they cannot disagree.
    NOT_IN_STANDING = ('dropped',)

    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='challenge_participations',
        db_index=True
    )
    challenge = models.ForeignKey(
        Challenge,
        on_delete=models.CASCADE,
        related_name='participations',
        db_index=True
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='joined',
        db_index=True
    )
    
    # Progress tracking
    current_value = models.FloatField(default=0)
    progress_percentage = models.FloatField(default=0)
    
    # Timestamps
    joined_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    # Ranking
    rank = models.PositiveIntegerField(null=True, blank=True)

    #: The review of this participant's standing (owner, 2026-09-24, F20).
    #:
    #: `clear` — nothing to look at (the default).
    #: `flagged` — a recount found a single day above a review threshold
    #:     (`social/challenge_progress.py REVIEW_THRESHOLDS`, reason `day_over_threshold`).
    #:     The person keeps their place, but a flagged first place wins nothing until an
    #:     operator clears it. Never un-flagged automatically.
    #: `disqualified` — an operator's decision (reason `operator`). The participation
    #:     holds NO place anywhere (`social/standing.py in_standing`), cannot rejoin
    #:     (`join` answers 403 `challenge_disqualified`), and its `current_value` is
    #:     still recounted, so clearing it restores a true number.
    REVIEW_CLEAR = 'clear'
    REVIEW_FLAGGED = 'flagged'
    REVIEW_DISQUALIFIED = 'disqualified'
    REVIEW_STATES = [
        (REVIEW_CLEAR, gettext_lazy('Clear')),
        (REVIEW_FLAGGED, gettext_lazy('Flagged for review')),
        (REVIEW_DISQUALIFIED, gettext_lazy('Disqualified')),
    ]
    REASON_DAY_OVER_THRESHOLD = 'day_over_threshold'
    REASON_OPERATOR = 'operator'
    REVIEW_REASONS = [
        (REASON_DAY_OVER_THRESHOLD, gettext_lazy('A day above the review threshold')),
        (REASON_OPERATOR, gettext_lazy('Set by an operator')),
    ]
    review_state = models.CharField(max_length=16, choices=REVIEW_STATES,
                                    default=REVIEW_CLEAR, db_index=True)
    review_reason = models.CharField(max_length=24, choices=REVIEW_REASONS,
                                     null=True, blank=True)
    #: The last training day an operator's CLEAR covered: the latest day above a review
    #: threshold when they cleared (`social/review.py apply_review`). A recount flags a
    #: cleared participant again only for a day above a threshold AFTER this one, so
    #: one clear is not a licence to log the daily maximum from then on. Null: the
    #: operator cleared nothing, and any day above a threshold flags.
    review_cleared_through = models.DateField(null=True, blank=True)

    class Meta:
        unique_together = ['user', 'challenge']
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-joined_at', '-id']
        indexes = [
            models.Index(fields=['challenge', 'status', 'current_value']),
            models.Index(fields=['user', 'status']),
            # Matches the real access pattern: WHERE user=? ORDER BY joined_at DESC, id DESC.
            # Measured on a power user with 5,050 rows: 0.682 ms -> 0.089 ms, because
            # the planner stops sorting their entire history to return 25 rows.
            models.Index(fields=['user', '-joined_at', '-id'], name='challengepa_recent_idx'),
        ]
        verbose_name = "Challenge Participation"
        verbose_name_plural = "Challenge Participations"
    
    def __str__(self):
        return f"{self.user.username} in {self.challenge.title}"


class Leaderboard(models.Model):
    """
    Track leaderboards for various metrics
    """
    LEADERBOARD_TYPES = [
        ('workout_streaks', 'Workout Streaks'),
        ('calories_burned', 'Calories Burned'),
        ('weight_loss', 'Weight Loss'),
        ('muscle_gain', 'Muscle Gain'),
        ('challenge_wins', 'Challenge Wins'),
        ('posts_likes', 'Most Liked Posts'),
        ('followers', 'Most Followers'),
        ('achievements', 'Most Achievements'),
    ]
    
    PERIOD_TYPES = [
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
        ('yearly', 'Yearly'),
        ('all_time', 'All Time'),
    ]
    
    leaderboard_type = models.CharField(
        max_length=30,
        choices=LEADERBOARD_TYPES,
        db_index=True
    )
    period_type = models.CharField(
        max_length=20,
        choices=PERIOD_TYPES,
        db_index=True
    )
    
    # Time period
    period_start = models.DateTimeField(db_index=True)
    period_end = models.DateTimeField(db_index=True)
    
    # Leaderboard data (JSON with rankings)
    rankings = models.JSONField(default=list)
    
    # Metadata
    total_participants = models.PositiveIntegerField(default=0)
    last_updated = models.DateTimeField(auto_now=True, db_index=True)
    
    class Meta:
        unique_together = ['leaderboard_type', 'period_type', 'period_start']
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-period_start', '-id']
        indexes = [
            models.Index(fields=['leaderboard_type', 'period_type']),
            models.Index(fields=['period_start', 'period_end']),
        ]
        verbose_name = "Leaderboard"
        verbose_name_plural = "Leaderboards"
    
    def __str__(self):
        return f"{self.get_leaderboard_type_display()} - {self.get_period_type_display()}"


class Achievement(models.Model):
    """
    User achievements and badges
    """
    ACHIEVEMENT_CATEGORIES = [
        ('workout', 'Workout Achievements'),
        ('diet', 'Diet Achievements'),
        ('social', 'Social Achievements'),
        ('challenge', 'Challenge Achievements'),
        ('streak', 'Streak Achievements'),
        ('milestone', 'Milestone Achievements'),
    ]
    
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField()
    category = models.CharField(
        max_length=20,
        choices=ACHIEVEMENT_CATEGORIES,
        db_index=True
    )
    
    # Achievement criteria
    criteria = models.JSONField(default=dict)
    points = models.PositiveIntegerField(default=10)
    
    # Media
    icon = models.ImageField(
        upload_to=achievement_icon_upload_path,
        blank=True,
        null=True
    )
    badge_color = models.CharField(max_length=7, default='#FFD700')  # Gold default
    
    # Rarity
    is_rare = models.BooleanField(default=False)
    is_secret = models.BooleanField(default=False)
    
    # Status
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['category', 'name']
        indexes = [
            models.Index(fields=['category', 'is_active']),
            models.Index(fields=['is_rare', 'is_active']),
        ]
        verbose_name = "Achievement"
        verbose_name_plural = "Achievements"
    
    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"


class UserAchievement(models.Model):
    """
    Track user achievements
    """
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='achievements',
        db_index=True
    )
    achievement = models.ForeignKey(
        Achievement,
        on_delete=models.CASCADE,
        related_name='user_achievements',
        db_index=True
    )
    earned_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    # Achievement context
    progress_data = models.JSONField(default=dict, blank=True)
    
    class Meta:
        unique_together = ['user', 'achievement']
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-earned_at', '-id']
        indexes = [
            models.Index(fields=['user', 'earned_at']),
            models.Index(fields=['achievement', 'earned_at']),
            # Matches the real access pattern: WHERE user=? ORDER BY earned_at DESC, id DESC.
            # Measured on a power user with 5,050 rows: 0.682 ms -> 0.089 ms, because
            # the planner stops sorting their entire history to return 25 rows.
            models.Index(fields=['user', '-earned_at', '-id'], name='socialachv_recent_idx'),
        ]
        verbose_name = "User Achievement"
        verbose_name_plural = "User Achievements"
    
    def __str__(self):
        return f"{self.user.username} earned {self.achievement.name}"


