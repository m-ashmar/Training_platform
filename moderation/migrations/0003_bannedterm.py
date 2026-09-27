"""The banned-terms list (spec B1: a basic banned-terms filter on post and comment
creation). A new table only; no existing row changes. The starter list is seeded by the
next migration, `0004_seed_banned_terms`.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("moderation", "0002_report_target_coach_message_challenge"),
    ]

    operations = [
        migrations.CreateModel(
            name="BannedTerm",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("term", models.CharField(max_length=100)),
                (
                    "normalized",
                    models.CharField(editable=False, max_length=200, unique=True),
                ),
                (
                    "whole_word",
                    models.BooleanField(
                        default=True,
                        help_text='On: the term must stand as its own word ("cunt" does not match "Scunthorpe"). Off: it matches inside other words too ("fuck" matches "motherfucker"): only for stems no innocent word contains.',
                    ),
                ),
                (
                    "language",
                    models.CharField(
                        choices=[
                            ("ar", "Arabic"),
                            ("en", "English"),
                            ("any", "Any / mixed"),
                        ],
                        default="any",
                        help_text="For the operator's own sorting. Every term is matched in every text.",
                        max_length=3,
                    ),
                ),
                (
                    "category",
                    models.CharField(
                        choices=[
                            ("obscenity", "Obscenity or sexual"),
                            ("insult", "Insult"),
                            ("hate", "Slur or hate"),
                            ("spam", "Spam or contact-for-sale"),
                            ("other", "Other"),
                        ],
                        default="other",
                        max_length=10,
                    ),
                ),
                (
                    "notes",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Why it is listed, where it was seen.",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text="Off: kept in the list, matched nowhere.",
                    ),
                ),
                (
                    "needs_review",
                    models.BooleanField(
                        default=False,
                        help_text="Seeded, and not yet reviewed by the owner.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Banned term",
                "verbose_name_plural": "Banned terms",
                "ordering": ["normalized", "id"],
            },
        ),
    ]
