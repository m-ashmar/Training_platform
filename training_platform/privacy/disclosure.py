"""The words the data inventory is written in: who receives personal data, and what kind
of data it is on the two stores' forms.

Spec §9 B3: "A data inventory ... listing every field collected, why, and who receives
it. It feeds the privacy policy, Apple's privacy labels and Play's Data safety form,
which must all agree." Every registered source (`sources.py`) declares its `purpose`,
its `recipients` and its `data_types` with the keys below; `manage.py data_inventory`
writes `docs/store/data_inventory.md` from them, and `tests/test_store_docs.py` holds
that file, `docs/store/privacy_labels.md` and the privacy-policy drafts to the same
list.

Two tables, one per question the stores ask, so a source says WHO and WHAT with keys
and the sentences live in one place:

* `RECIPIENTS` — who, outside the platform's own servers and operators, receives the
  data. Other people in the app (a coach, the community) and the service providers
  that process it for the platform. `PROCESSORS` are the providers that hold
  EVERYTHING (the servers, the database), listed once rather than on every source.
* `DATA_TYPES` — the data type on each store's form: Apple's App Privacy category
  ("Contact Info > Email Address") and Google Play's Data safety type ("Personal info >
  Email address"). `None` on both means the source is not declared on the forms, and
  `not_declared` says why (operators' own records, which no app user gives).

`name` and `name_ar` are the plain words the privacy policy uses for the data type;
the test checks each one in use appears in `docs/legal/privacy_policy_{en,ar}.md`.

What is written here must be true of the code — each line says where to look. A change
of provider (the AI coach, email, storage) is a change here, then `manage.py
data_inventory`, then the policy.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Recipient:
    #: Who, in a few words.
    name: str
    #: What they receive, and when.
    receives: str
    #: "person" — someone else using the app, shown it because of what the user did;
    #: "service provider" — processes it for the platform and for nothing else.
    kind: str


RECIPIENTS: dict[str, Recipient] = {
    "coach": Recipient(
        "The client's trainer",
        "While a coaching is approved: the profile a coach works from (name, email, photo, "
        "age, gender, height, weight, goals, activity level, training answers, health "
        "notes and injury areas), the client's training log, diet plan, nutrition record "
        "and food preferences, and their chat. A trainer with only a pending request sees "
        "a short row and never health notes (`users/trainer_requests.py`). A client's "
        "training and nutrition are the trainer's to read only through the approved "
        "relation (`CustomUser.can_access_user_data`).",
        "person"),
    "clients": Recipient(
        "The trainer's clients",
        "What a trainer writes for the clients they coach: routines, diet plans, chat "
        "messages, challenges they set, and the trainer's profile and prices on a build "
        "that may show them.",
        "person"),
    "community": Recipient(
        "Other signed-in users",
        "What a person publishes: username, name and photo, posts, comments, likes, "
        "follows, challenges, leaderboard places and badges. Hidden from anyone they "
        "blocked or who blocked them (`moderation.services.blocked_ids`); a suspended "
        "account leaves every public surface.",
        "person"),
    "public": Recipient(
        "Anyone, without signing in",
        "The public trainer directory (`GET /api/v1/trainers/`, `trainers/public/`): a "
        "trainer's card — name, photo, bio, specialisations, certifications, experience "
        "and the proof numbers, the price only on a build that may show it.",
        "person"),
    "agent": Recipient(
        "The agent who tops a wallet up",
        "The client's name and account number and the amount, on the top-up and its "
        "receipt (`wallet/agents.py person`). Agents are accounts the platform creates, "
        "never self-registered.",
        "person"),
    "ai_provider": Recipient(
        "The AI service provider",
        "Only after the person grants AI consent (`moderation.AIConsent`; every AI route "
        "refuses without it): the messages of the conversation, and what the coach reads "
        "to answer — the profile (name, age, gender, height, weight, BMI, goals, injury "
        "note, activity level, the trainer's name), workouts and set logs, the routine, "
        "the diet plan and meals, and the coach's own notes about the person "
        "(`ai_assistant/services/context_compiler.py`, `ai_assistant/tools/`). The app "
        "names no provider (the owner's decision, 2026-09-24); the one configured is "
        "OpenAI (`OPENAI_API_KEY`, `OPENAI_MODEL`). Nothing else is sent to an AI: diet "
        "plans are built by the platform's own rules.",
        "service provider"),
    "push_provider": Recipient(
        "Google Firebase Cloud Messaging",
        "The device's push token and the title and text of each push "
        "(`notifications/channels/fcm.py`), which can carry a person's name and the "
        "first words of a chat message.",
        "service provider"),
    "email_provider": Recipient(
        "The email delivery provider",
        "The email address and the sign-up or password-reset code — the only emails the "
        "server sends (`users/utils.py`). The provider is the owner's choice "
        "(`EMAIL_HOST`).",
        "service provider"),
    "file_storage": Recipient(
        "Google Cloud Storage",
        "The files people upload: profile photos, trainers' media, chat photos, voice "
        "notes and videos, post and challenge images, and trainers' verification "
        "documents (a separate private bucket). Production refuses to start without it "
        "(`MEDIA_BACKEND=gcs`, `settings_production.py`).",
        "service provider"),
    "payment_gateway": Recipient(
        "ShamCash",
        "For a payment: its reference, amount and currency (`subscription/gateways/"
        "shamcash.py`); the platform reads the transfers its own merchant account "
        "received. For a trainer's withdrawal: the trainer's ShamCash address and the "
        "amount, which an operator sends through ShamCash.",
        "service provider"),
}


@dataclass(frozen=True)
class Processor:
    name: str
    what: str


#: Service providers that hold EVERY source, because they run the platform. Listed once
#: in the inventory, not on each source.
PROCESSORS: tuple[Processor, ...] = (
    Processor("Fly.io", "Runs the servers (`fly.toml`, region Amsterdam). Every request "
              "and every stored row passes through it."),
    Processor("The PostgreSQL and Redis providers", "Hold the database and the caches. "
              "Chosen by the owner at deployment."),
    Processor("Sentry (optional)", "Server error reports, only when `SENTRY_DSN` is set, "
              "and with `send_default_pii=False` (no email, no request body, no IP)."),
    Processor("AWS Secrets Manager (optional)", "Holds the server's own secrets when "
              "`AWS_SECRET_NAME` is set. No personal data."),
)


@dataclass(frozen=True)
class DataType:
    #: The plain words for it, used verbatim by `docs/legal/privacy_policy_en.md`.
    name: str
    #: The same, used verbatim by `docs/legal/privacy_policy_ar.md`.
    name_ar: str
    #: App Store Connect > App Privacy: "Category > Data type". None = not declared.
    apple: str | None
    #: Play Console > Data safety: "Category > Data type". None = not declared.
    google: str | None
    #: Why neither form lists it, when both are None.
    not_declared: str = ""


DATA_TYPES: dict[str, DataType] = {
    "name": DataType("Name", "الاسم",
                     "Contact Info > Name", "Personal info > Name"),
    "email": DataType("Email address", "البريد الإلكتروني",
                      "Contact Info > Email Address", "Personal info > Email address"),
    "phone": DataType("Phone number", "رقم الهاتف",
                      "Contact Info > Phone Number", "Personal info > Phone number"),
    "address": DataType("Shop location of an agent", "موقع متجر الوكيل",
                        "Contact Info > Physical Address", "Personal info > Address"),
    "user_ids": DataType("Account identifiers", "معرّفات الحساب",
                         "Identifiers > User ID", "Personal info > User IDs"),
    "device_ids": DataType("Device push token", "رمز الإشعارات للجهاز",
                           "Identifiers > Device ID", "Device or other IDs > Device or other IDs"),
    "other_personal": DataType("Date of birth, age and gender", "تاريخ الميلاد والعمر والجنس",
                               "Other Data > Other Data Types", "Personal info > Other info"),
    "health": DataType("Health information", "المعلومات الصحية",
                       "Health & Fitness > Health", "Health and fitness > Health info"),
    "fitness": DataType("Fitness and training activity", "النشاط الرياضي والتدريب",
                        "Health & Fitness > Fitness", "Health and fitness > Fitness info"),
    "purchases": DataType("Purchase and payment history", "سجل المشتريات والمدفوعات",
                          "Purchases > Purchase History", "Financial info > Purchase history"),
    "financial": DataType("Wallet and payout details", "بيانات المحفظة والسحب",
                          "Financial Info > Other Financial Info",
                          "Financial info > Other financial info"),
    "messages": DataType("Chat messages", "رسائل المحادثة",
                         "User Content > Emails or Text Messages",
                         "Messages > Other in-app messages"),
    "ai_conversations": DataType("Conversations with the AI coach", "المحادثات مع المدرب الذكي",
                                 "User Content > Other User Content",
                                 "Messages > Other in-app messages"),
    "photos_videos": DataType("Photos and videos", "الصور ومقاطع الفيديو",
                              "User Content > Photos or Videos",
                              "Photos and videos > Photos, Videos"),
    "audio": DataType("Voice notes", "الرسائل الصوتية",
                      "User Content > Audio Data", "Audio > Voice or sound recordings"),
    "user_content": DataType("Posts, comments and other content you create",
                             "المنشورات والتعليقات وما تنشئه من محتوى",
                             "User Content > Other User Content",
                             "App activity > Other user-generated content"),
    "support": DataType("Reports and requests you send us", "البلاغات والطلبات التي ترسلها إلينا",
                        "User Content > Customer Support",
                        "App activity > Other user-generated content"),
    "documents": DataType("Identity and qualification documents", "وثائق الهوية والمؤهلات",
                          "Other Data > Other Data Types", "Files and docs > Files and docs"),
    "app_activity": DataType("App activity", "النشاط داخل التطبيق",
                             "Usage Data > Product Interaction", "App activity > App interactions"),
    "connection": DataType("IP address and device details", "عنوان IP وبيانات الجهاز",
                           "Diagnostics > Other Diagnostic Data",
                           "Device or other IDs > Device or other IDs"),
    "credentials": DataType(
        "Sign-in codes", "رموز تسجيل الدخول", None, None,
        not_declared="Security secrets (one-time codes, reset tokens, API keys), stored "
                     "hashed or encrypted and used only to sign in; neither form has a "
                     "category for them."),
    "staff_records": DataType(
        "Operator records", "سجلات المشغّلين", None, None,
        not_declared="About the platform's own operators, who work in the web console, "
                     "not the app. No app user gives this data."),
}
