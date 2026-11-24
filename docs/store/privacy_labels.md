# Privacy labels: App Store and Google Play answers

For the owner, when filling **App Store Connect > App Privacy** and **Play Console > App
content > Data safety**. Written from `data_inventory.md` (generated from the privacy
registry) and from the code named in each line. Spec §9 B3 and §11: the privacy policy,
these labels and the Data safety form must list the same data.
`tests/test_store_docs.py` fails when a data type the inventory declares is missing
here or from the privacy-policy drafts.

Written 2026-09-27 against `spec/payments-compliance`. Re-read before every submission:
a new registered source, a new SDK in the app or a new provider changes the answers.

Marks used below:

- **[APP]** the answer depends on the Flutter app (`yalla_gym/`), which a backend
  session does not build. The app session confirms it on the build being submitted.
- **[OWNER]** a choice or a provider the owner decides; the answer given is the one
  that is true today.

## 1. Facts the answers rest on

| Question | Answer | Where it is true |
|---|---|---|
| Advertising, ad SDKs, tracking, data brokers | None. No ad or tracking SDK, pixel or attribution service in the backend (searched for Facebook, AppsFlyer, Adjust, Mixpanel, Segment, Amplitude, Google Analytics, AdMob, IDFA / advertising IDs, Branch, OneSignal, PostHog: no match outside tests). No data is sold or given to anyone for their own use. | `requirements.txt`; the only external script in any page is chart.js in one operator console page |
| Health data used for advertising or data mining (Apple 5.1.3) | No. It is used to plan and track the person's own training and nutrition, shown to their own trainer, and sent to the AI provider only after consent. | `data_inventory.md`, recipients table |
| Who else receives data | The person's trainer, other users (only what the person publishes), and service providers that process it for the platform: the hosting provider (Hetzner; the database and Redis run on its server), the file storage provider (Hetzner Object Storage), Firebase Cloud Messaging, the email provider, ShamCash, the AI provider (after consent), Sentry (optional). | `training_platform/privacy/disclosure.py` |
| Encrypted in transit | Yes, on the server: HTTP is redirected to HTTPS and HSTS is sent. **[APP]** the release build calls only `https://` and `wss://` addresses. | `settings_production.py` (`SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS`), `deploy/caddy/Caddyfile` (Caddy serves HTTPS only and redirects HTTP), `fly.toml` (`force_https`) |
| Encrypted at rest (beyond the provider's disk encryption) | Encrypted columns: a client's injury note and injury areas, the note sent with a coaching request, a trainer's ShamCash address and holder name (and each withdrawal's copy of them), an agent's API secret. Hashed: passwords, sign-up codes, reset codes. Identity documents sit in a private bucket and are served only as five-minute signed links. | `training_platform/encrypted_fields.py`, `users/models.py`, `payouts/models.py`, `wallet/models.py` `AgentAPIKey`, `trainer_verification/storage.py` |
| Account deletion in the app | Yes: `DELETE /api/privacy/erase/?confirm=ERASE` (preview with `GET`). **[APP]** reachable at Me > Privacy and data > Delete my account, as the web page says. | `training_platform/privacy/views.py` |
| Account deletion without the app | Yes: `https://<PUBLIC_PAGES_BASE_URL>/legal/delete-account/?lang=ar` (and `?lang=en`). The app receives the same URL as `links.account_deletion` from `GET /api/v1/config/capabilities`. | `legal/views.py`, `legal/links.py` |
| Deleting some data without deleting the account | Yes: posts and comments (`DELETE /api/social/posts/{id}/`, `comments/{id}/`), the AI coach's chats, notes and usage records (`DELETE /api/ai/data/`), the anonymous training copies (switching "help us improve" off). Not a deletion: a chat message the sender takes back is hidden from the thread and kept (for reports). | `social/`, `ai_assistant/views.py`, `chat/models.py` |
| A copy of one's data | Yes: `GET /api/privacy/export/`, a JSON file of every registered source. | `training_platform/privacy/views.py` |
| Minimum age | 14, enforced by the server from the date of birth at sign-up (`under_minimum_age`). | `users/serializers.py`, `MINIMUM_AGE_YEARS` |
| Location | None collected. The server never derives a location, and since 2026-11-24 it never stores one either: `POST /api/analytics/sessions/` ignores `country`, `region` and `city` (read-only, always empty; SIDE_BUGS S-221). | `analytics/serializers.py` `UserSessionSerializer` |

## 2. Switches that change these answers

| Switch | Today | If it changes |
|---|---|---|
| `SENTRY_DSN` | **[OWNER]** unset or set | When set: server error reports, without personal data (`send_default_pii=False`). Declare Apple "Diagnostics > Crash Data" (not linked, App Functionality) only if the app also sends crash reports **[APP]**; the server's own error reports are not data the app collects from the device. |
| `DIET_EXTERNAL_FOOD_SEARCH` | off | When on, the text of a food search is sent to Edamam, with no account identifier and not stored by the platform. Consider Apple "Search History" (not linked) / Google "App activity > In-app search history". |
| The AI provider (`AI_BASE_URL`, `AI_ASSISTANT_MODEL`, `AI_API_KEY`) | Cloudflare Workers AI, running the open model Gemma (the owner, 2026-11-17) | A new provider changes `disclosure.py` `ai_provider`, then the inventory and the policy. Its terms must forbid using the data for its own purposes, or Google's "shared" answer changes to Yes. **[OWNER]** |
| `firebase_messaging` in the app | not in `pubspec.yaml` on 2026-09-27 **[APP]** | When push is wired, the push token is collected (already declared below as Device ID). |

## 3. Apple: App Privacy

**Do you or your third-party partners collect data from this app?** Yes.

**Tracking.** No data type is used for tracking. The app does not link its data with
other companies' data for advertising, and shares nothing with data brokers. No
`NSUserTrackingUsageDescription`, no App Tracking Transparency prompt.
**[APP]** true only while no ad, attribution or analytics SDK is in the build.

Every type below is **Linked to the user** (it is stored against the account) and
**Not used for tracking**. Purposes use Apple's names.

| Apple data type | Purposes | What it is (inventory keys) |
|---|---|---|
| Contact Info > Name | App Functionality | First and last name, a payout holder's name, an agent's listing name (`name`) |
| Contact Info > Email Address | App Functionality | Sign-in, sign-up and password-reset codes; the web deletion request (`email`) |
| Contact Info > Phone Number | App Functionality | Asked at sign-up; an agent's public shop phone (`phone`) |
| Contact Info > Physical Address | App Functionality | An agent's city and area, on the listing agents agree to (`address`) |
| Identifiers > User ID | App Functionality | Username and account number (`user_ids`) |
| Identifiers > Device ID | App Functionality | The push token of each phone (`device_ids`) |
| Health & Fitness > Health | App Functionality, Product Personalization | Height, weight, injuries, allergies, body-weight history, the body measurements a person logs (chest, arm, waist, thigh), the allergen check of a plan (`health`) |
| Health & Fitness > Fitness | App Functionality, Product Personalization | Workouts, sets, routines, diet plans, meals ticked, goals, challenges (`fitness`) |
| Purchases > Purchase History | App Functionality | Plan payments, coaching months, programs bought, the ledger (`purchases`) |
| Financial Info > Other Financial Info | App Functionality | Wallet balance, a trainer's ShamCash address, withdrawals, an agent's cash record (`financial`) |
| User Content > Emails or Text Messages | App Functionality | The trainer–client chat (`messages`) |
| User Content > Photos or Videos | App Functionality | Profile photo, trainers' media, chat photos and videos, post, challenge and program images (`photos_videos`) |
| User Content > Audio Data | App Functionality | Voice notes in the chat (`audio`) |
| User Content > Other User Content | App Functionality, Product Personalization | Posts, comments, challenges, a trainer's routines and bio; conversations with the AI coach (`user_content`, `ai_conversations`) |
| User Content > Customer Support | App Functionality | Reports of content or people; the reason given on the web deletion request (`support`) |
| Usage Data > Product Interaction | App Functionality, Analytics, Product Personalization | Activity log, app sessions, badges, likes, follows, blocks, consent answers, the AI coach's habit notes (`app_activity`) |
| Diagnostics > Other Diagnostic Data | App Functionality, Analytics | IP address and device string on activity rows, app sessions, the wallet audit trail and an agent's allowed addresses (`connection`) |
| Other Data > Other Data Types | App Functionality | Date of birth, age and gender (`other_personal`); a trainer's identity and qualification documents (`documents`) |

**Not collected:** Location (**[APP]** see §1), Contacts, Browsing History, Search
History (while `DIET_EXTERNAL_FOOD_SEARCH` is off), Sensitive Info, Crash Data and
Performance Data (**[APP]** no crash SDK), Advertising Data, Payment Info (no card or
bank number is ever entered; ShamCash is paid outside the app), Credit Info, Gameplay
Content, Environment Scanning, Hands, Head, Body, Emails or Text Messages other than
the chat.

Judgment calls, for the owner to confirm or change:

- IP address and device string are declared as "Other Diagnostic Data": Apple has no
  IP category; the server uses them for security (the wallet audit trail) and counts.
- Identity documents and date of birth are "Other Data Types": Apple's "Sensitive
  Info" list (race, religion, biometrics...) does not include them.
- The anonymous AI training copies (`ai_training_data`) are declared with the AI
  conversations as linked: they carry no account, but the server can find them by a
  keyed pseudonym (to delete them), so "not linked" would overstate it.

## 4. Google Play: Data safety

**Overview answers**

| Question | Answer |
|---|---|
| Does your app collect or share any of the required user data types? | Yes |
| Is all of the user data collected by your app encrypted in transit? | Yes (§1; **[APP]** https and wss only) |
| Which of the following methods of account creation does your app support? | Username and password (the email address is the username; the address is confirmed with an emailed code at sign-up). **[OWNER]** Play also asks for the delete-account URL here. |
| Do you provide a way for users to request that their data is deleted? | Yes: in the app, and at `https://<PUBLIC_PAGES_BASE_URL>/legal/delete-account/` |
| Delete account URL | `https://<PUBLIC_PAGES_BASE_URL>/legal/delete-account/?lang=ar` |
| Can users request that some data be deleted without deleting the account? | Yes (§1) |
| Independent security review | No **[OWNER]** |
| Committed to Play Families Policy | No: the app is not for children; the minimum age is 14 |

**Shared: No, for every type.** Google counts as "sharing" a transfer to a third party
for its own use. Every recipient here is either a service provider processing for the
platform (the hosting provider, the file storage provider, Firebase, the email
provider, ShamCash, the AI provider) or another user shown data by the person's own
action (publishing a post, asking a trainer to coach them), which Google excludes.
**[OWNER]** keep the AI provider's terms on file: if they allow the provider to use the
data for itself, the AI conversation and health types become "shared".

**Processed ephemerally: No** for every type: all of it is stored.

| Google data type | Required or optional | Purposes | Inventory keys |
|---|---|---|---|
| Personal info > Name | Optional | App functionality, Account management | `name` |
| Personal info > Email address | Required | App functionality, Account management | `email` |
| Personal info > Phone number | Required (asked at sign-up) | App functionality, Account management | `phone` |
| Personal info > Address | Optional (agents only) | App functionality | `address` |
| Personal info > User IDs | Required | App functionality, Account management | `user_ids` |
| Personal info > Other info | Required (date of birth) | App functionality, Account management, Personalization | `other_personal` |
| Financial info > Purchase history | Optional | App functionality, Fraud prevention, security, and compliance | `purchases` |
| Financial info > Other financial info | Optional | App functionality, Fraud prevention, security, and compliance | `financial` |
| Health and fitness > Health info | Optional | App functionality, Personalization | `health` |
| Health and fitness > Fitness info | Optional | App functionality, Personalization | `fitness` |
| Messages > Other in-app messages | Optional | App functionality, Personalization | `messages`, `ai_conversations` |
| Photos and videos > Photos | Optional | App functionality | `photos_videos` |
| Photos and videos > Videos | Optional | App functionality | `photos_videos` |
| Audio > Voice or sound recordings | Optional | App functionality | `audio` |
| Files and docs > Files and docs | Optional (trainers asking to be verified) | App functionality, Fraud prevention, security, and compliance | `documents` |
| App activity > App interactions | Required | App functionality, Analytics, Personalization | `app_activity` |
| App activity > Other user-generated content | Optional | App functionality | `user_content`, `support` |
| Device or other IDs > Device or other IDs | Required (IP address and device string are logged with activity) | App functionality, Developer communications (push), Fraud prevention, security, and compliance, Analytics | `device_ids`, `connection` |

**Not collected:** Location, Web browsing, Contacts, Calendar, App info and performance
(**[APP]** no crash SDK), Financial info > User payment info and Credit score, Health
and fitness beyond the two above, Personal info > Race and ethnicity, Political or
religious beliefs, Sexual orientation, In-app search history (while
`DIET_EXTERNAL_FOOD_SEARCH` is off), Installed apps, Emails, SMS or MMS.

## 5. What the app session must confirm on the build being submitted

1. `pubspec.yaml` has no advertising, attribution, analytics or crash-reporting SDK. On
   2026-09-27 it had none (dio, riverpod, go_router, secure storage, shared
   preferences, image_picker, sqflite, connectivity, cached images, share, url_launcher,
   timezone, web_socket_channel, qr_flutter).
2. `firebase_messaging` (when added) registers the token through
   `POST /api/auth/device-token/` only after the person allows notifications.
3. No call to `POST /api/analytics/sessions/` sends `country`, `region` or `city` (the
   server ignores them since 2026-11-24, so this is tidiness, not a label question).
4. The release build's base URL is `https://` and the sockets are `wss://`.
5. Me > Privacy and data offers "Download my data" (`/api/privacy/export/`) and "Delete
   my account" (`/api/privacy/erase/`), and opens the policy, terms and deletion page
   from `links` in the capabilities answer.
6. The AI consent sheet says, before anything is sent, that an outside AI service
   provider receives the conversation and the profile, training and diet data it reads,
   and that consent can be withdrawn in settings (Apple 5.1.2(i)).
7. The camera, photo library and microphone permission texts (iOS `Info.plist`) say
   why: a profile photo, chat photos and videos, voice notes, verification documents.
8. What is stored on the phone (secure storage, shared preferences, sqflite) stays on
   the phone and is not sent anywhere else.
