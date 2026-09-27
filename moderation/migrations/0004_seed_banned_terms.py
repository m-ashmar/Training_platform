"""The starter banned-terms list — MODEST, and every row marked `needs_review`.

Written so the filter refuses the obvious on day one (spec B1). The owner reviews each
term before launch in the console (`/dj-admin/moderation/bannedterm/?needs_review__exact=1`):
keep it (untick "needs review"), change it, or switch it off. A term an operator adds
is theirs and never marked.

What is here, and what deliberately is not:

* Arabic (Levantine and general) and English obscenities, insults, slurs and hate terms
  that have no everyday innocent meaning. Words that do — "كلب" (dog), "حمار" (donkey),
  "خول" (also "authorised"), "رافضة" (also "refusing", feminine), "نيك" (also the name
  Nick), "dick" (a name) — are left out: a filter that refuses ordinary sentences
  teaches people to evade it.
* Contact-for-sale spam: WhatsApp and Telegram links, and the phrases that move a sale
  off the platform ("للتواصل واتس", "dm for price", steroids and followers for sale).
* Whole word unless the term is a stem no innocent word contains (`whole_word` off:
  "fuck" in "motherfucker"). "shit" stays whole-word: as a stem it matches "shiitake".

Idempotent: a term already listed (by its folded form) is skipped, so re-applying after
a rollback adds nothing. Reversing removes only seeded rows still waiting for review.
"""
from django.db import migrations

#: (term, whole_word, language, category, note)
SEED = (
    # --- English: obscenity -------------------------------------------------------
    ("fuck", False, "en", "obscenity", "Stem: fucking, fucker, motherfucker."),
    ("shit", True, "en", "obscenity", "Whole word: as a stem it matches 'shiitake'."),
    ("bullshit", True, "en", "obscenity", ""),
    ("bitch", False, "en", "obscenity", "Stem: bitches, bitchy."),
    ("cunt", True, "en", "obscenity", "Whole word: 'Scunthorpe'."),
    ("whore", True, "en", "obscenity", ""),
    ("slut", True, "en", "obscenity", ""),
    ("asshole", False, "en", "obscenity", ""),
    ("dickhead", True, "en", "obscenity", ""),
    ("pussy", True, "en", "obscenity", "Also 'cat': review."),
    ("porn", False, "en", "obscenity", "Stem: porno, pornography."),
    # --- English: slurs and hate --------------------------------------------------
    ("nigger", False, "en", "hate", "Racist slur."),
    ("nigga", True, "en", "hate", "Racist slur."),
    ("faggot", False, "en", "hate", "Homophobic slur."),
    ("kike", True, "en", "hate", "Antisemitic slur."),
    ("retard", True, "en", "hate", "Ableist slur."),
    ("tranny", True, "en", "hate", "Transphobic slur."),
    # --- English: contact-for-sale spam -------------------------------------------
    ("wa.me/", True, "any", "spam", "WhatsApp chat link."),
    ("chat.whatsapp.com", False, "any", "spam", "WhatsApp group invite link."),
    ("t.me/", True, "any", "spam", "Telegram link."),
    ("dm for price", True, "en", "spam", ""),
    ("whatsapp me", True, "en", "spam", ""),
    ("buy followers", True, "en", "spam", ""),
    ("steroids for sale", True, "en", "spam", ""),
    ("onlyfans", False, "en", "spam", "Adult-content promotion."),
    # --- Arabic: obscenity --------------------------------------------------------
    ("شرموطة", True, "ar", "obscenity", ""),
    ("شرموط", True, "ar", "obscenity", ""),
    ("شراميط", True, "ar", "obscenity", ""),
    ("منيوك", True, "ar", "obscenity", ""),
    ("منيوكة", True, "ar", "obscenity", ""),
    ("قحبة", True, "ar", "obscenity", ""),
    ("قحاب", True, "ar", "obscenity", ""),
    ("كس امك", True, "ar", "obscenity", ""),
    ("كسمك", True, "ar", "obscenity", ""),
    ("كس اختك", True, "ar", "obscenity", ""),
    ("طيز", True, "ar", "obscenity", ""),
    ("زب", True, "ar", "obscenity", "Two letters: matched only as its own word."),
    ("انيكك", True, "ar", "obscenity", ""),
    # --- Arabic: insults ----------------------------------------------------------
    ("عرص", True, "ar", "insult", ""),
    ("معرص", True, "ar", "insult", ""),
    ("ديوث", True, "ar", "insult", ""),
    ("ابن الحرام", True, "ar", "insult", ""),
    ("يلعن ربك", True, "ar", "insult", "Blasphemous curse."),
    ("يلعن دينك", True, "ar", "insult", "Blasphemous curse."),
    # --- Arabic: slurs and hate ---------------------------------------------------
    ("لوطي", True, "ar", "hate", "Homophobic slur."),
    ("زنجي", True, "ar", "hate", "Racist slur."),
    ("نصيري", True, "ar", "hate", "Sectarian slur. Review for the Syrian context."),
    ("نصيرية", True, "ar", "hate", "Sectarian slur. Review for the Syrian context."),
    ("رافضي", True, "ar", "hate", "Sectarian slur. 'رافضة' is left out: it is also "
                                  "'refusing' (feminine)."),
    ("روافض", True, "ar", "hate", "Sectarian slur."),
    # --- Arabic: contact-for-sale spam --------------------------------------------
    ("للتواصل واتس", False, "ar", "spam", "Stem: واتس / واتساب / واتس اب."),
    ("راسلني واتس", False, "ar", "spam", "Stem: واتس / واتساب."),
    ("للطلب واتس", False, "ar", "spam", "Stem: واتس / واتساب."),
    ("تواصل خاص", True, "ar", "spam", "Moving a sale off the platform."),
    ("راسلني خاص", True, "ar", "spam", "Moving a sale off the platform."),
    ("زيادة متابعين", True, "ar", "spam", ""),
    ("متابعين للبيع", True, "ar", "spam", ""),
    ("هرمونات للبيع", True, "ar", "spam", ""),
    ("ابر تنشيف", True, "ar", "spam", "Fat-loss injections sold off the platform."),
    ("ستيرويد للبيع", True, "ar", "spam", ""),
)


def _normalise(term):
    # The filter's own folding, so the stored form is the one the console compares.
    from moderation.ugc_filter import normalise

    return normalise(term)


def seed_banned_terms(apps, schema_editor):
    BannedTerm = apps.get_model("moderation", "BannedTerm")
    listed = set(BannedTerm.objects.values_list("normalized", flat=True))
    for term, whole_word, language, category, note in SEED:
        folded = _normalise(term)
        if not folded or folded in listed:
            continue
        BannedTerm.objects.create(
            term=term, normalized=folded, whole_word=whole_word, language=language,
            category=category, notes=note, is_active=True, needs_review=True)
        listed.add(folded)


def unseed_banned_terms(apps, schema_editor):
    BannedTerm = apps.get_model("moderation", "BannedTerm")
    BannedTerm.objects.filter(
        needs_review=True,
        normalized__in=[_normalise(term) for term, *_rest in SEED]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("moderation", "0003_bannedterm"),
    ]

    operations = [
        migrations.RunPython(seed_banned_terms, unseed_banned_terms),
    ]
