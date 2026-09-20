"""Text messages between a trainer and the client they coach (the owner, 2026-09-20).

Texting only in this pass: no voice, no image, no video, no program card. The seams for
those are named where they belong — `may_message` decides WHO may write, and a future
attachment is a field on `Message` — but none of them is built here.

Two tables, and the shape of each one is what keeps the screens cheap:

    Conversation   exactly one per (trainer, client) pair, enforced by a unique
                   constraint rather than by whoever writes the next get-or-create.
                   `last_message_at` orders the list; the two `..._last_read_at`
                   stamps make an unread count a COUNT over one index instead of a
                   walk through the thread.
    Message        append-only in practice: a sender may tombstone their own message
                   (`deleted_at`) and nothing hard-deletes a row, because the other
                   person's thread is not the sender's to rewrite. `client_uuid` is the
                   app's own id for the message, so a retried send returns the first row
                   instead of writing a second one (the same pattern as
                   `routine.ExerciseSetLog.client_uuid`, scoped to the conversation).

A frozen, unpaid or ended coaching does NOT touch these rows: the owner's rule is that an
unpaid relationship is never deleted, so the thread stays readable for ever and only
SENDING is refused (`chat/permissions.py`).
"""
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

#: What the serializer accepts in one message. Long enough for a coach's paragraph,
#: short enough that a thread page stays a sensible size on a phone.
MAX_BODY_CHARS = 4000


class Conversation(models.Model):
    """One thread, one (trainer, client) pair.

    `last_message_at` starts at the moment the thread was opened rather than NULL: the
    list is ordered by it, and a nullable sort column puts an empty conversation in
    whatever position the database feels like (Postgres sorts NULLs first on DESC).
    """
    trainer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='chat_as_trainer', limit_choices_to={'user_type': 'trainer'})
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='chat_as_client', limit_choices_to={'user_type': 'client'})
    created_at = models.DateTimeField(auto_now_add=True)
    #: The moment of the newest message, or of creation while there is none.
    last_message_at = models.DateTimeField(default=timezone.now, db_index=True)
    #: When each side last opened the thread. Everything the OTHER side wrote after
    #: this is unread, which is one indexed COUNT per conversation. Both start at the
    #: moment the thread was opened rather than NULL — there is nothing to have missed
    #: yet — so the unread count is a plain comparison and never a COALESCE.
    trainer_last_read_at = models.DateTimeField(default=timezone.now)
    client_last_read_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Conversation'
        verbose_name_plural = 'Conversations'
        # `id` tiebreaker: a single non-unique sort column is not a total order, so
        # LIMIT/OFFSET paging repeats and hides rows whenever two records share a value.
        ordering = ['-last_message_at', '-id']
        constraints = [
            # A constraint, not a convention: two phones tapping "message" at the same
            # moment both read "no conversation yet" and both insert. The database
            # decides, and `chat.services.open_conversation` reads the winner back.
            models.UniqueConstraint(fields=['trainer', 'client'],
                                    name='unique_chat_conversation_per_pair'),
        ]
        indexes = [
            # The two list screens, exactly as they are read: mine, newest first.
            models.Index(fields=['trainer', '-last_message_at'], name='chat_conv_trainer_idx'),
            models.Index(fields=['client', '-last_message_at'], name='chat_conv_client_idx'),
        ]

    def __str__(self):
        return f"Conversation {self.pk}: trainer {self.trainer_id} / client {self.client_id}"

    def other_id(self, user_id):
        """The id of the person on the other side, or None for anyone else."""
        if user_id == self.trainer_id:
            return self.client_id
        if user_id == self.client_id:
            return self.trainer_id
        return None

    def read_field_for(self, user_id):
        """Which `..._last_read_at` column is this caller's, or None."""
        if user_id == self.trainer_id:
            return 'trainer_last_read_at'
        if user_id == self.client_id:
            return 'client_last_read_at'
        return None


class Message(models.Model):
    """One text message. Text only in this pass — there is no attachment field yet."""
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='chat_messages')
    #: Stored exactly as it was typed. The length cap lives in the serializer; nothing
    #: here rewrites a person's words.
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    #: The app's own id for this message: the first send with it is created, a retry
    #: whose answer was lost returns that same row instead of posting twice. Scoped to
    #: the conversation, so two threads cannot collide over one phone's counter.
    client_uuid = models.UUIDField(null=True, blank=True)
    #: When the recipient opened the thread after this arrived.
    read_at = models.DateTimeField(null=True, blank=True)
    #: The sender took it back. The ROW stays: the other person's thread is not the
    #: sender's to rewrite, and a hard delete would renumber a cursor mid-scroll.
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Message'
        verbose_name_plural = 'Messages'
        # `id` tiebreaker, and the same order the thread is paged in.
        ordering = ['-created_at', '-id']
        constraints = [
            # Partial, because only a message that CARRIES the app's id has one to
            # collide on; a message sent without it is simply not replayable.
            models.UniqueConstraint(
                fields=['conversation', 'client_uuid'],
                condition=Q(client_uuid__isnull=False),
                name='unique_chat_client_uuid_per_conversation'),
        ]
        indexes = [
            # The thread page: WHERE conversation = ? ORDER BY id DESC LIMIT n.
            models.Index(fields=['conversation', '-id'], name='chat_msg_thread_idx'),
        ]

    def __str__(self):
        return f"Message {self.pk} in conversation {self.conversation_id}"
