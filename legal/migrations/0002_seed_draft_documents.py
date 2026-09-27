"""Version 1 of the privacy policy and the terms, in Arabic and English: an OBVIOUS DRAFT.

The owner writes and approves the real text (a separate documentation pass drafts it);
until then `/legal/privacy/` and `/legal/terms/` must answer — the capabilities endpoint
already hands their URLs to every build — and must say, on their face, that they are a
placeholder. The approved text is published from the console as version 2, which
replaces these on the pages at once.

Written through the historical model, so `published_by` is empty (no operator did it).
Re-running is harmless: a (kind, language) that already has any version is left alone.
"""
from django.db import migrations
from django.utils import timezone

SEED = {
    ("privacy", "ar"): (
        "مسودة — سياسة الخصوصية (نص مؤقت، غير معتمد)",
        "## مسودة: هذا النص مؤقت\n\n"
        "هذه مسودة مؤقتة وليست سياسة الخصوصية المعتمدة. ستُنشر السياسة المعتمدة هنا "
        "قبل إتاحة التطبيق، وتحلّ محل هذا النص.\n\n"
        "## ما ستتضمنه السياسة المعتمدة\n\n"
        "- البيانات التي نجمعها ولماذا نجمعها\n"
        "- من يطّلع عليها، ومنهم مدربك المعتمد ومزوّد خدمة المدرب الذكي بعد موافقتك\n"
        "- أن بياناتك الصحية لا تُستخدم للإعلانات\n"
        "- مدة الاحتفاظ بها، وكيف تنزّل نسخة منها أو تحذف حسابك",
    ),
    ("privacy", "en"): (
        "DRAFT — Privacy policy (placeholder, not approved)",
        "## DRAFT: this text is a placeholder\n\n"
        "This is a temporary draft, not the approved privacy policy. The approved policy "
        "will be published here before the app is released, and will replace this text.\n\n"
        "## What the approved policy will cover\n\n"
        "- What data we collect and why\n"
        "- Who sees it, including your approved trainer and, with your consent, the "
        "provider of the AI coach\n"
        "- That your health data is never used for advertising\n"
        "- How long it is kept, and how to download a copy or delete your account",
    ),
    ("terms", "ar"): (
        "مسودة — شروط الاستخدام (نص مؤقت، غير معتمد)",
        "## مسودة: هذا النص مؤقت\n\n"
        "هذه مسودة مؤقتة وليست شروط الاستخدام المعتمدة. ستُنشر الشروط المعتمدة هنا "
        "قبل إتاحة التطبيق، وتحلّ محل هذا النص.",
    ),
    ("terms", "en"): (
        "DRAFT — Terms of use (placeholder, not approved)",
        "## DRAFT: this text is a placeholder\n\n"
        "This is a temporary draft, not the approved terms of use. The approved terms "
        "will be published here before the app is released, and will replace this text.",
    ),
}


def seed(apps, schema_editor):
    LegalDocument = apps.get_model("legal", "LegalDocument")
    now = timezone.now()
    for (kind, language), (title, body) in SEED.items():
        if LegalDocument.objects.filter(kind=kind, language=language).exists():
            continue
        LegalDocument.objects.create(kind=kind, language=language, version=1, title=title,
                                     body=body, is_published=True, published_at=now)


class Migration(migrations.Migration):

    dependencies = [
        ("legal", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
