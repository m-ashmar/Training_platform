"""Messages between a trainer and the client they coach (the owner, 2026-09-20).

Text first (2026-09-20), then a photo, a voice note and a video (BACKEND_BUGS B-091,
2026-09-24): one attachment per message, on the message itself (`kind`, `attachment`,
`duration_seconds`, `width`, `height`), with `body` as its caption. A client's video
that names an `exercise` is a FORM CHECK — "watch my deadlift" — and waits in the
trainer's attention queue (`users/attention_queue.py`) until the trainer writes in that
thread after it. A video gets a POSTER frame after it is stored (`poster`, cut off the
request by `chat.tasks.cut_video_poster` where the worker has ffmpeg).

A PROGRAM CARD (`kind='program'`, 2026-09-27) is the trainer pointing at a routine they
wrote for this client — «حدّث برنامجك · برنامج القوة · يوم الدفع» — with an optional
day and caption: `routine`, `program_day`, and `program_changes`, what the card said
about the routine when it was sent. The client opens it as the routine it names, and
sees it only while they may READ that routine (`routine.permissions.readable_routines`,
the rule the routine routes use).

The bytes go through the platform's one upload pipeline (`chat/media.py`): the same
content sniffing and caps as the media kit, stored on the same backend (GCS in
production, the filesystem in tests), and served only as a short-lived signed URL
inside the answers the two people in the thread receive. Nothing else links to them.

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
import os
import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

#: What the serializer accepts in one message. Long enough for a coach's paragraph,
#: short enough that a thread page stays a sensible size on a phone.
MAX_BODY_CHARS = 4000

#: What a message IS. The app switches on it before reading anything else, and draws a
#: value it does not know as a placeholder (BACKEND_BUGS B-042). Add values; never
#: rename one.
KIND_TEXT, KIND_IMAGE, KIND_VOICE, KIND_VIDEO = 'text', 'image', 'voice', 'video'
#: A trainer's card pointing at one of this client's routines (2026-09-27).
KIND_PROGRAM = 'program'
KIND_CHOICES = [(KIND_TEXT, 'Text'), (KIND_IMAGE, 'Image'), (KIND_VOICE, 'Voice'),
                (KIND_VIDEO, 'Video'), (KIND_PROGRAM, 'Program')]
MEDIA_KINDS = (KIND_IMAGE, KIND_VOICE, KIND_VIDEO)


def _attachment_upload_to(instance, filename):
    """`chat/<conversation>/<kind>-<random>.<ext>`.

    The extension is the one the upload pipeline DETECTED (`chat/media.py` names the
    file); nothing the client called it reaches storage. The random token makes the
    path unguessable, and the signature on every URL makes it useless without the
    answer that carried it.
    """
    ext = os.path.splitext(filename)[1].lower() or '.bin'
    return (f"chat/{instance.conversation_id}/"
            f"{instance.kind}-{secrets.token_hex(12)}{ext}")


def _poster_upload_to(instance, filename):
    """`chat/<conversation>/poster-<random>.jpg`: beside the video it was cut from,
    unguessable like it, and served only signed, like it."""
    return f"chat/{instance.conversation_id}/poster-{secrets.token_hex(12)}.jpg"


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
    #: When each side was last PUSHED about this thread (`chat.events.notify_new_message`).
    #: The nudge rule — tell someone again only once they have opened the thread since
    #: the last push — is claimed against these with one conditional UPDATE at send time.
    #: It used to be read from the last `chat_message` Notification row, which a Celery
    #: worker writes LATER: a burst sent faster than the queue drained pushed once per
    #: message, and a read between the send and the worker's write swallowed the next
    #: legitimate push (SIDE_BUGS S-095). NULL = never pushed.
    trainer_notified_at = models.DateTimeField(null=True, blank=True)
    client_notified_at = models.DateTimeField(null=True, blank=True)

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
    """One message: text, a photo, voice note or video, or a trainer's program card —
    the last three with an optional caption.

    A media message keeps its file for as long as the row lives, tombstone included —
    the same rule as the text, which stays in the column after the sender takes it
    back: the row is a record (a report may need it), and no endpoint serves either
    once `deleted_at` is set. The file goes when the ROW goes, removed once the delete
    commits — but NOT by a per-row delete receiver. Such a receiver switches off
    Django's fast delete for this table, so erasing a coach would load every message
    of every thread into memory to delete it (review 2026-09-24). The files are swept
    by the two paths that delete messages in bulk instead: a conversation's own delete
    (`chat.media.delete_conversation_files`, a `pre_delete` on `Conversation`, which
    the cascade from an erased or deleted account also fires) and the erasure of the
    messages a person sent (`training_platform/privacy/sources.py`). Deleting a
    Message row on its own — nothing in the product does — leaves its file.
    """
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='chat_messages')
    #: Stored exactly as it was typed. The length cap lives in the serializer; nothing
    #: here rewrites a person's words. On a media message it is the caption, and may be
    #: empty.
    body = models.TextField(blank=True)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=KIND_TEXT)
    #: The photo (re-encoded, so no EXIF or GPS survives), the voice note or the video
    #: (stored as uploaded, after its container was checked from its own bytes). Empty
    #: on a text message. Private: served only through `media_url()`, signed.
    attachment = models.FileField(upload_to=_attachment_upload_to, max_length=255,
                                  blank=True)
    #: Voice and video: what the container declares, read without decoding. Null when
    #: it declares nothing readable (a WebM, an Ogg) — never a number the client sent.
    duration_seconds = models.FloatField(null=True, blank=True)
    #: A photo's pixel size, so the app can lay the bubble out before it loads.
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    #: What this message keeps in storage, in bytes: the stored file's size (a photo's
    #: re-encode, a voice note or video as uploaded). Null on text, and on a media row
    #: written before this column existed. Summed by the sender's daily media quota
    #: (`chat.media.require_media_quota`) so the sum is exact, not read back from the
    #: bucket.
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    #: The move a video shows. A client's video that names one is a form check.
    exercise = models.ForeignKey(
        'routine.Exercise', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+')
    #: A video's poster: one JPEG frame near its start, cut by a worker after the video
    #: is stored (`chat.tasks.cut_video_poster`), never on the request. Empty until
    #: then, and for good where the worker has no ffmpeg or could not read the video.
    #: Private like the video, served signed (`chat.media.payload` `poster_url`), and
    #: removed with it (`chat.media.stored_names`).
    poster = models.FileField(upload_to=_poster_upload_to, max_length=255, blank=True)
    #: A program card's routine: one the trainer wrote that this client is on. Null on
    #: every other kind, and on a card whose routine was deleted since (the card stays
    #: in the thread and opens nothing).
    routine = models.ForeignKey(
        'routine.Routine', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+')
    #: The day of that routine the card opens (1-based), or null for the whole routine.
    program_day = models.PositiveSmallIntegerField(null=True, blank=True)
    #: What the card said about the routine WHEN IT WAS SENT, written once:
    #: `{"exercises": n, "changes": null | {"since", "added", "edited", "removed"}}`
    #: (`chat.services.program_summary`). A snapshot, so a page of history reads no
    #: routine-exercise rows to draw it.
    program_changes = models.JSONField(null=True, blank=True)
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
            # The trainer's attention queue: the form checks of a roster. Partial, so it
            # holds only the few rows that are videos naming an exercise.
            models.Index(fields=['conversation', 'id'], name='chat_msg_formcheck_idx',
                         condition=Q(kind='video', exercise__isnull=False,
                                     deleted_at__isnull=True)),
            # The sender's daily media quota: their media in the last 24 hours. Partial,
            # so a coach's thousands of text messages are not in it.
            models.Index(fields=['sender', 'created_at'], name='chat_msg_media_quota_idx',
                         condition=Q(kind__in=MEDIA_KINDS)),
        ]

    def __str__(self):
        return f"Message {self.pk} in conversation {self.conversation_id}"
