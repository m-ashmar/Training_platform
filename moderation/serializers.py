"""What the app sends and what it gets back.

Read-only on the way out: a report's status, who handled it and when are the operator's
to write, never the reporter's. The write serializers validate SHAPE only — whether the
reporter may see the target, and whether they are reporting themselves, are decisions
about other apps' rows and live in `moderation/views.py` with the resolver that answers
them.
"""
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from .models import AIConsent, Block, Report


class ReportCreateSerializer(serializers.Serializer):
    """`{target_type, target_id, reason, note?}`.

    `target_type` and `reason` are closed sets; anything else is
    `validation_error` with the offending field in `field_errors`.
    """

    target_type = serializers.ChoiceField(choices=Report._meta.get_field("target_type").choices)
    target_id = serializers.IntegerField(min_value=1)
    reason = serializers.ChoiceField(choices=Report._meta.get_field("reason").choices)
    note = serializers.CharField(
        required=False, allow_blank=True, default="",
        max_length=Report._meta.get_field("note").max_length, trim_whitespace=True)


class ReportSerializer(serializers.ModelSerializer):
    """One of the caller's own reports, and what came of it.

    `handled_by` is the operator's id and is deliberately absent — the reporter learns
    the outcome, not who decided it.
    """

    class Meta:
        model = Report
        fields = ["id", "target_type", "target_id", "reason", "note", "status",
                  "resolution_note", "handled_at", "created_at"]
        read_only_fields = fields


class BlockedUserSerializer(serializers.ModelSerializer):
    """A row in `GET blocks/`: who, and since when.

    The blocked person's username is included because the list exists to be undone —
    a screen of bare ids is not a screen anyone can act on.
    """

    user_id = serializers.IntegerField(source="blocked_id", read_only=True)
    username = serializers.CharField(source="blocked.username", read_only=True)

    class Meta:
        model = Block
        fields = ["user_id", "username", "created_at"]
        read_only_fields = fields


class BlockCreateSerializer(serializers.Serializer):
    """`{user_id}` — the account to hide."""

    user_id = serializers.IntegerField(min_value=1)


class AIConsentSerializer(serializers.ModelSerializer):
    """The person's standing answer about the AI coach."""

    granted = serializers.BooleanField(source="is_active", read_only=True)

    class Meta:
        model = AIConsent
        fields = ["granted", "policy_version", "granted_at", "revoked_at"]
        read_only_fields = fields


class AIConsentWriteSerializer(serializers.Serializer):
    """`{granted, policy_version}`.

    `policy_version` is required when granting and ignored when revoking: a revocation
    withdraws every version at once, so asking which one is being withdrawn would only
    invite a client to send the wrong answer.
    """

    granted = serializers.BooleanField()
    policy_version = serializers.CharField(
        required=False, allow_blank=True, default="",
        max_length=AIConsent._meta.get_field("policy_version").max_length)

    def validate(self, attrs):
        if attrs["granted"] and not attrs.get("policy_version", "").strip():
            raise serializers.ValidationError({"policy_version": _(
                "Say which version of the policy is being agreed to.")})
        return attrs
