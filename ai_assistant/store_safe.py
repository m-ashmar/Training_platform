"""What the AI coach may say about money depends on the BUILD, and on nothing else.

Spec §10 lists what never ships in a store build, and one line of it is ours: "a support
bot that answers 'how do I pay' with instructions inside a store build". Spec §3 says
what "not sold in app" means: no price beside a call to action, no "subscribe on our
website", no link to the hub. The coach is a language model, so a rule in its prompt is
a request, not a guarantee. This module is the guarantee, in three parts:

1. **Which build is asking** — `build_of_scope` for the socket, `build_of_request` for
   REST. Both read the SAME two declarations `policy.channels` reads (`X-App-Channel`,
   `X-App-Version`), with the same parser, so an unknown or missing channel is what it
   is on every REST route: `None`, which policy answers with `STRICTEST`.
2. **Whether that build may talk about paying** — `payment_talk_blocked`, from
   `policy.services.flags_for` and nothing else: a build that may not buy BOTH
   store-billed kinds of money (`policy.money.STORE_BILLED_TYPES`, via policy's own
   `PURCHASE_FLAG`) is store-safe. Today that is `play`, `ios` and a caller that
   declares nothing; `apk` and `web` are not. No channel name is written here, and an
   operator row that stops a build selling plans makes it store-safe from its version.
3. **What the coach answers** — `is_payment_question` recognises a question about
   paying, prices, subscribing or buying, in Arabic (standard and Levantine), Arabizi
   and English, and on a store-safe build `chat_service.chat_stream` answers it with
   `store_safe_reply()` WITHOUT calling the model. What the recogniser misses reaches
   the model with `STORE_BUILD_RULES` in its system prompt.

Rule zero. The inputs are the build and the words of the question. Never the user, the
country, the IP, the date or a review mode: every user of a build asking the same thing
gets the same answer, a reviewer included.

**Nothing post-filters the model.** Its reply is streamed and saved exactly as written
on every build. Editing a reply after the fact means matching money words inside it,
and those words are everywhere in ordinary coaching ("pay attention to your knees",
"the price of ego lifting", "free weights", "تمارين الدفع" is push training): a filter
strong enough to catch a payment instruction would mangle normal answers, and a filter
weak enough not to would catch nothing. So the decision is taken on the QUESTION,
before the model runs, and on a store build the reply to a recognised question is the
fixed sentence in full — never a reply with parts cut out. The REST history applies the
same decision to the question each stored reply answered (`store_safe_reply_ids`), so a
reply given on the direct APK is not read back on a store build either; every other
reply is shown untouched.
"""
from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import parse_qs

from django.utils.translation import gettext as _

from ai_assistant.anonymise import _fold
from policy.channels import CHANNEL_HEADER, VERSION_HEADER, channel_of, version_of

#: The query parameters a socket may declare its build with when its client cannot set
#: a header on a WebSocket handshake (a browser). The header wins when both are sent.
CHANNEL_QUERY = "app_channel"
VERSION_QUERY = "app_version"


# ------------------------------------------------------------------ 1. the build
class _Declared:
    """What a handshake declared, shaped like a request so `policy.channels` reads it
    exactly as it reads a REST call: one parser, one idea of a valid channel."""

    def __init__(self, channel: str, version: str):
        self.headers = {CHANNEL_HEADER: channel, VERSION_HEADER: version}


def build_of_scope(scope) -> tuple:
    """`(channel, version)` a WebSocket declared on its handshake.

    `X-App-Channel` / `X-App-Version` as handshake headers, or `?app_channel=` /
    `?app_version=` in the query string beside `?token=`. A header that is present and
    not blank is the declaration; otherwise the query parameter is. The values are
    judged by `policy.channels.channel_of` / `version_of`: exact lower-case channels
    only, so `IOS`, `iphone` or nothing at all is `None`, never a guess.
    """
    scope = scope or {}
    headers = {}
    for name, value in scope.get("headers") or []:
        if isinstance(name, bytes) and isinstance(value, bytes):
            headers[name.decode("latin-1").lower()] = value.decode("latin-1").strip()
    query = parse_qs((scope.get("query_string") or b"").decode("latin-1", "ignore"))

    def declared(header, parameter):
        return headers.get(header.lower()) or (query.get(parameter) or [""])[0]

    request = _Declared(declared(CHANNEL_HEADER, CHANNEL_QUERY),
                        declared(VERSION_HEADER, VERSION_QUERY))
    return channel_of(request), version_of(request)


def build_of_request(request) -> tuple:
    """`(channel, version)` a REST request declared — `policy.channels`, unchanged."""
    return channel_of(request), version_of(request)


def payment_talk_blocked(channel, version) -> bool:
    """True when this build may not sell the store-billed goods, so the coach may not
    talk about paying. Reads `policy.services.flags_for(channel, version)` — the flags
    the capabilities endpoint answers — and takes no user: that signature is the
    guarantee that two users of one build get one answer."""
    from policy.money import STORE_BILLED_TYPES
    from policy.services import PURCHASE_FLAG, flags_for

    flags = flags_for(channel, version)
    return not all(flags[PURCHASE_FLAG[kind]] for kind in STORE_BILLED_TYPES)


# ------------------------------------------------------------------ 2. what is said
def store_safe_reply() -> str:
    """The coach's whole answer to a payment question on a store-safe build.

    No price, no instruction, no place, no link, and no hint that paying happens
    somewhere else (spec §3, §10): it says what the coach is for.
    """
    return str(_("I'm the AI coach, so I can't help with payments, prices or "
                 "subscriptions. Ask me about your training, your meals or your "
                 "progress."))


#: The system prompt's rule on a store-safe build, for the questions the recogniser
#: does not know. English, like the rest of the prompt; the model answers in the
#: reader's language.
STORE_BUILD_RULES = (
    "# THIS APP BUILD SELLS NOTHING (immutable)",
    "- This version of the app sells no plans, subscriptions, coaching or programmes, "
    "and you never help anyone buy them.",
    "- NEVER give prices, costs, fees, discounts or amounts of money for any plan, "
    "subscription, coaching, trainer or programme.",
    "- NEVER explain how to pay, subscribe, upgrade, renew or top up, and NEVER suggest "
    "paying on a website, through an agent, by a transfer or in any other way.",
    "- NEVER include links or web addresses.",
    "- If asked about any of this, say you are the AI coach and can only help with "
    "training, meals and progress.",
)


# ------------------------------------------------------------------ 3. the recogniser
#: Letters some keyboards type in place of the Arabic ones (Persian yeh and kaf, the
#: Kurdish and the knotted heh). `anonymise._fold` already drops the diacritics and
#: the tatweel, folds أ إ آ ٱ to ا, ة to ه, ى to ي, ؤ to و, ئ to ي, and case.
_EXTRA_FOLD = str.maketrans({"ی": "ي", "ک": "ك", "ە": "ه", "ھ": "ه"})

#: Phrases whose money word is not about money, cut out before the words are read.
_NOT_MONEY = tuple(re.compile(pattern) for pattern in (
    r"\bpa(?:y|ys|id|ying)\s+(?:(?:close|more|extra|special|careful|no|any|some|much|"
    r"little|full)\s+)?attention\b",                       # pay attention
    r"\bpa(?:y|ys|id|ying)\s+off\b",                        # my work paid off
    r"\b(?:in|take|takes|took|taking)\s+charge\b",          # who is in charge
    r"\b(?:calorie|calories|caloric|energy|metabolic|oxygen)\s+costs?\b",
    r"سعر\w*\s+(?:ال)?حراري\w*",                            # السعر الحراري: a calorie
))

#: One-letter proclitics (و ف ب ل ك) and the article, stripped off an Arabic word so
#: "وبالاشتراك", "للاشتراك" and "الاشتراك" all read as "اشتراك"; the Levantine
#: b- of the present ("بدفع", "بشترك") goes the same way.
_PREFIXES = ("ال", "و", "ف", "ب", "ل", "ك")
#: Words that merely BEGIN with a proclitic letter and would turn into a money word
#: without it: "بروتين" (protein) is not ب + "روتين" (routine).
_WHOLE_WORDS = frozenset({"بروتين", "بروتينات", "بروتيني"})

# Every word below is written FOLDED (no hamza on alef, ة as ه, ى as ي).

#: Alone, a question about paying. English is unambiguous once "pay attention" and
#: "paid off" are gone.
_PAY_EN = frozenset({"pay", "pays", "paying", "paid", "payment", "payments", "repay",
                     "prepaid", "paypal"})
#: The Arabic verb دفع is also "push" (تمارين الدفع = push training, ادفع الوزن = push
#: the weight), so it counts only beside money words or away from exercise words.
_PAY_AR = re.compile(r"^(?:[اينت]|من)?دفع(?:ت|تي|تو|تم|نا|و|وا|ها|هم|ه|لك|لي|له|لكم|لو)?$")
_PAY_AMBIGUOUS = frozenset({
    "مدفوع", "مدفوعه", "مدفوعات", "تسديد", "اسدد", "سددت", "بسدد",
    "adfa3", "edfa3", "bedfa3", "badfa3", "nedfa3", "bnedfa3", "dfa3", "dafa3t",
    "2dfa3", "2edfa3", "idfa3",
})
#: Alone, a question about subscribing, billing or the wallet.
_BILLING = frozenset({
    "subscribe", "subscribes", "subscribed", "subscribing", "subscription",
    "subscriptions", "unsubscribe", "membership", "memberships", "checkout", "billing",
    "billed", "refund", "refunds", "refunded", "invoice", "invoices", "shamcash",
    "wallet", "wallets", "topup", "discount", "discounts", "coupon", "coupons", "promo",
    "promocode",
    "شامكاش", "محفظه", "محفظتي", "محفظتك", "رصيد", "رصيدي", "رصيدك", "فاتوره", "فواتير",
    "فاتورتي", "ايصال", "سداد", "كوبون",
    "eshterak", "eshtirak", "ishtirak", "eshtrak", "ishtrak", "2eshterak", "2eshtirak",
    "eshtarek", "ashtrek", "bshtrek", "beshtrek",
})
#: Arabic "subscribe": also "take part", as in joining a challenge, so beside a
#: challenge word and no money word it is not a purchase.
_SUBSCRIBE_AR = frozenset({
    "اشتراك", "اشتراكات", "اشتراكي", "اشتراكك", "اشتراكه", "اشتراكها", "اشتراكو",
    "اشتراكنا", "اشتراكهم", "اشترك", "اشتركت", "اشتركنا", "اشتركو", "اشتركوا", "يشترك",
    "تشترك", "نشترك", "شترك", "منشترك",
})
_CHALLENGE = frozenset({"تحدي", "تحديات", "تحديي", "مسابقه", "مسابقات", "سباق", "بطوله",
                        "challenge", "challenges"})
#: A price, a fee, a currency: money in any context, but a question about OUR money
#: only beside "how much", a product, or a website.
_PRICE = frozenset({
    "price", "prices", "pricing", "priced", "cost", "costs", "costing", "fee", "fees",
    "syp", "lira", "liras", "dollar", "dollars", "usd", "bucks", "$", "£", "€",
    "سعر", "اسعار", "سعرها", "سعرهم", "سعرو", "سعرك", "ثمن", "ثمنه", "ثمنها", "تكلفه",
    "تكاليف", "كلفه", "كلفته", "كلفتها", "يكلف", "تكلف", "رسوم", "مبلغ", "ليره",
    "ليرات", "دولار", "دولارات", "خصم",
    "se3r", "s3r", "as3ar", "2as3ar", "taklfe", "taklife", "taklifa",
})
#: Cheap, dear, money, free: about our money only beside one of our products or a
#: website ("a cheap meal plan" is a nutrition question).
_CHEAP = frozenset({
    "cheap", "cheaper", "cheapest", "expensive", "afford", "affordable", "money", "cash",
    "free", "charge", "charges", "charged",
    "رخيص", "رخيصه", "ارخص", "غالي", "غاليه", "اغلي", "مصاري", "مصريات", "فلوس", "نقود",
    "كاش", "مجاني", "مجانيه", "مجانا", "ببلاش", "مشترك", "مشتركين", "استرداد", "استرجاع",
    "masari", "masare", "msari", "floos", "flous", "ra5ees", "ar5as", "majjani",
    "majani", "bbalash",
})
_HOW_MUCH = frozenset({
    "كم", "قديش", "قدي", "اديش", "قداش", "شقد", "كام",
    "adesh", "2adesh", "2desh", "addesh", "adeish", "2adeish", "b2adesh", "bi2adesh",
    "qadesh", "kam", "bkam",
})
#: The things the platform prices by name: its plans and their tiers.
_TIER = frozenset({
    "premium", "premuim", "بريميوم", "باقه", "باقات", "باقتي", "باقتك", "احترافيه",
    "بروفيشنال", "بيسك", "عضويه", "features",
})
_TIER_PAIRS = frozenset({"pro plan", "professional plan", "basic plan", "premium plan",
                         "ai coach", "مدرب ذكي"})
#: Everything the platform (or a trainer on it) sells or is.
_PRODUCT = _TIER | frozenset({
    "app", "application", "coaching", "coach", "coaches", "trainer", "trainers", "yalla",
    "trial", "trials",
    "تطبيق", "مدرب", "مدربي", "مدربك", "مدربين", "مدربه", "كوتش",
})
#: Words that name a product only sometimes ("plan" is also a diet plan).
_MAYBE_PRODUCT = frozenset({
    "plan", "plans", "program", "programs", "programme", "programmes", "routine",
    "routines", "session", "sessions", "course", "courses",
    "خطه", "خطتي", "خطط", "برنامج", "برامج", "برنامجي", "روتين", "جلسه", "جلسات", "كورس",
    "دوره",
})
#: Somewhere else to pay: the spec's "subscribe on our website" (§3).
_ELSEWHERE = frozenset({
    "website", "websites", "site", "web", "online", "link", "links", "url", "hub",
    "browser", "موقع", "رابط", "لينك", "ويب", "اونلاين",
})
_BUY = frozenset({
    "buy", "buying", "bought", "purchase", "purchases", "purchased", "purchasing",
    "اشتري", "شتري", "نشتري", "تشتري", "يشتري", "منشتري", "اشتريت", "شراء",
    "ashtri", "eshtri", "bshtri", "beshtri",
})
#: Getting a plan: about our money beside a tier ("upgrade to premium"), and not
#: otherwise ("upgrade my squat", "renew my routine").
_UPGRADE = frozenset({
    "upgrade", "upgrading", "unlock", "unlocking", "activate", "get",
    "renew", "renewal", "renewing", "renewed",
    "ترقيه", "رقي", "ارقي", "رقيت", "تفعيل", "افتح", "فتح",
    "تجديد", "جدد", "اجدد", "جددت", "جدده", "يتجدد",
})
#: Training words: beside them, the Arabic دفع is a push, not a payment.
_EXERCISE = frozenset({
    "تمرين", "تمارين", "تمرينات", "تمريني", "تمرينك", "وزن", "اوزان", "بار", "دمبل",
    "دامبل", "دمبلز", "بنش", "صدر", "صدري", "كتف", "اكتاف", "رجل", "رجلي", "ارجل", "ساق",
    "سكوات", "سحب", "ضغط", "عضله", "عضلات", "حديد", "جهاز", "اجهزه", "مجموعه",
    "مجموعات", "جوله", "تكرار", "تكرارات", "عده", "عدات", "ركبه", "ركب", "ظهر", "ذراع",
    "ذراعي", "ايد", "ايدي", "يدي", "كابل", "حبل", "ارض", "اعلي", "فوق", "لفوق", "حوض",
    "ورك", "بطن", "بوش", "بريس", "فخذ", "كيلو", "كغ", "ثقل", "اثقال", "جسم", "جسمي",
    "حالي", "نفسي", "حركه", "وضعيه", "كره", "كوره", "قدم", "رفع",
    "push", "press", "bench", "weight", "weights", "bar", "barbell", "dumbbell",
    "dumbbells", "set", "sets", "rep", "reps", "squat", "squats", "lift", "lifting",
    "chest", "shoulder", "shoulders", "leg", "legs", "sled", "kg", "kilo", "kilos",
    "plate", "plates", "machine", "cable", "hip", "hips", "knee", "knees", "exercise",
    "exercises", "workout", "myself", "harder", "gym",
})
_ARABIC_LETTER = re.compile(r"[ء-ي]")


def _normalise(text: str) -> str:
    folded = _fold(text).translate(_EXTRA_FOLD)
    for pattern in _NOT_MONEY:
        folded = pattern.sub(" ", folded)
    return folded


def _stems(word: str) -> set:
    """The word and every reading of it with its Arabic proclitics taken off."""
    out = {word}
    if not _ARABIC_LETTER.match(word):
        return out
    frontier = [word]
    for _depth in range(3):
        found = []
        for current in frontier:
            if current in _WHOLE_WORDS:
                continue
            for prefix in _PREFIXES:
                rest = current[len(prefix):]
                if current.startswith(prefix) and len(rest) >= 2 and rest not in out:
                    out.add(rest)
                    found.append(rest)
        frontier = found
    return out


def _signals(text) -> set:
    """Which kinds of word `text` contains: the evidence `is_payment_question` weighs."""
    if not isinstance(text, str) or not text.strip():
        return set()
    words = re.findall(r"[^\W_]+|[$£€]", _normalise(text))
    stems = [_stems(word) for word in words]
    every = set().union(*stems) if stems else set()
    pairs = {f"{a} {b}" for first, second in zip(stems, stems[1:])
             for a in first for b in second}
    found = set()
    checks = (
        ("pay", _PAY_EN), ("pay_ambiguous", _PAY_AMBIGUOUS), ("billing", _BILLING),
        ("subscribe_ar", _SUBSCRIBE_AR), ("challenge", _CHALLENGE), ("price", _PRICE),
        ("cheap", _CHEAP), ("how_much", _HOW_MUCH), ("tier", _TIER),
        ("product", _PRODUCT), ("maybe_product", _MAYBE_PRODUCT),
        ("elsewhere", _ELSEWHERE), ("buy", _BUY), ("upgrade", _UPGRADE),
        ("exercise", _EXERCISE),
    )
    for name, lexicon in checks:
        if every & lexicon:
            found.add(name)
    if any(_PAY_AR.match(stem) for stem in every):
        found.add("pay_ambiguous")
    if pairs & {"how much"}:
        found.add("how_much")
    if pairs & {"top up", "credit card", "debit card", "شام كاش", "sham cash",
                "شحن رصيد", "كود خصم"}:
        found.add("billing")
    if pairs & _TIER_PAIRS:
        found.update({"tier", "product"})
    return found


def is_payment_question(text) -> bool:
    """Does this message ask about paying, prices, subscribing or buying?

    Tuned to be sure of the questions a person actually asks about OUR money ("how do
    I pay", "كم سعر الاشتراك", "قديش البريميوم", "kif bedfa3", "is it cheaper on the
    website") and to leave alone the money words of ordinary coaching ("how much
    protein", "a cheap meal plan", "pay attention", "calorie cost", "كم سعرة",
    "تمارين الدفع", "اشترك بالتحدي"). What it misses meets `STORE_BUILD_RULES`; what it
    catches wrongly gets a polite redirect, never a wrong answer.
    """
    s = _signals(text)
    money = bool(s & {"price", "cheap", "billing", "tier", "elsewhere"})
    if "billing" in s or "pay" in s:
        return True
    if "subscribe_ar" in s and not ("challenge" in s and not s & {"price", "cheap", "pay"}):
        return True
    if "pay_ambiguous" in s and (money or "exercise" not in s):
        return True
    if "price" in s and s & {"how_much", "product", "maybe_product", "elsewhere"}:
        return True
    if "cheap" in s and s & {"product", "elsewhere"}:
        return True
    if "how_much" in s and "tier" in s:
        return True
    if "buy" in s and s & {"product", "maybe_product"}:
        return True
    if "upgrade" in s and "tier" in s:
        return True
    if "elsewhere" in s and s & {"buy", "upgrade"}:
        return True
    return False


# ------------------------------------------------------------------ the REST history
def store_safe_reply_ids(messages) -> set:
    """The ids of the assistant replies among `messages` that answered a payment
    question: what the history shows as `store_safe_reply()` on a store-safe build.

    A reply's question is the newest user message of its chat written before it. A
    page can hold a reply without its question (cursor pages split anywhere), so the
    questions are read from the database: the one before the page's oldest reply, and
    every one up to its newest — two queries per chat, however long the chat is.
    """
    from django.db.models import Q

    from ai_assistant.models import ChatMessage

    replies = defaultdict(list)
    for message in messages:
        if message.role == "assistant":
            replies[message.session_id].append(message)

    blocked = set()
    for session_id, rows in replies.items():
        oldest = min(rows, key=lambda m: (m.created_at, m.id))
        newest = max(rows, key=lambda m: (m.created_at, m.id))
        asked = ChatMessage.objects.filter(session_id=session_id, role="user")
        before_oldest = (Q(created_at__lt=oldest.created_at)
                         | Q(created_at=oldest.created_at, id__lt=oldest.id))
        before_newest = (Q(created_at__lt=newest.created_at)
                         | Q(created_at=newest.created_at, id__lt=newest.id))
        questions = list(asked.filter(before_oldest).order_by("-created_at", "-id")
                         .values_list("created_at", "id", "content")[:1])
        questions += list(asked.filter(before_newest).exclude(before_oldest)
                          .order_by("created_at", "id")
                          .values_list("created_at", "id", "content"))
        questions.sort(key=lambda q: (q[0], q[1]))
        for reply in rows:
            earlier = [q for q in questions if (q[0], q[1]) < (reply.created_at, reply.id)]
            if earlier and is_payment_question(earlier[-1][2]):
                blocked.add(reply.id)
    return blocked
