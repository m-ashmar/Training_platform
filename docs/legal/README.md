# The privacy policy and terms: drafts, and how to publish an approved version

| File | What it is |
|---|---|
| `privacy_policy_en.md`, `privacy_policy_ar.md` | DRAFT privacy policy, built from `docs/store/data_inventory.md` |
| `terms_en.md`, `terms_ar.md` | DRAFT terms of use, built from what the server enforces |

They are drafts for the owner and a lawyer (spec §9 B7). Nothing here is published: the
public pages `/legal/privacy/` and `/legal/terms/` show version 1, an obvious
placeholder seeded by `legal/migrations/0002`, until an approved version is published
from the console. The drafts must stay true: when the product changes, change the
privacy registry, run `python manage.py data_inventory`, then the drafts
(`tests/test_store_docs.py` checks the policy still names every data type).

## Publishing an approved version

The console is `/dj-admin/`; only an operator (a superuser or an `admin` account with
the legal permissions) can open it (`legal/admin.py`). Do each language separately:
the Arabic privacy policy, the English one, then the same for the terms.

1. **Approve the text first.** The draft's header (everything above the line
   `===== BODY ... =====`) is not part of the text. Every `[OWNER: ...]`,
   `[LAWYER: ...]`, `[APP: ...]`, `[المالك: ...]` and `[المحامي: ...]` must be filled
   and its brackets removed. The console does not check this.
2. Open **Legal documents** (`/dj-admin/legal/legaldocument/`).
3. Tick the published version 1 of that kind and language, choose the action
   **Start a new draft from this version**, then open the new draft (it has the next
   version number). Or use **Add legal document** and pick the kind and the language.
4. **Title**: the page's heading, e.g. "Privacy policy" / "سياسة الخصوصية".
   **Body**: paste the approved text, from below the BODY line. The body is plain text:
   a blank line starts a paragraph, a line starting with `## ` is a heading, lines
   starting with `- ` are a list (one line per item), web and email addresses become
   links. No HTML, no bold: it is shown exactly as typed. No prices and no links or
   instructions to pay — every build opens this page, the store builds included.
5. **Save** without ticking "published" and read it once more in the form: a draft is
   not shown anywhere.
6. Tick **Is published** and save. From then on it is the version the public page
   shows (within five minutes: the page may be cached that long), and it can no longer
   be edited or deleted — a published version is the record of what people were shown.
   The next wording is a new version, started the same way (step 3).
7. Open the public page from the **Public page** link in the form:
   `/legal/privacy/?lang=ar`, `?lang=en`, `/legal/terms/?lang=ar`, `?lang=en`.

A language with no published version shows the other language's, with a line saying
so; a kind with none in either language answers 404.

## Settings the pages and the app read

| Setting | What it does |
|---|---|
| `PUBLIC_PAGES_BASE_URL` | The hub domain (e.g. `https://yallagym.com`) the links are built on. Unset, they use the host the request came to |
| `PLATFORM_CONTACT_EMAIL` | The contact address printed on every legal page. Empty prints none |
| `PLATFORM_NAME` | The trading name on the pages; empty means "Yalla Gym" / «يلا جيم» |
| `PLATFORM_LEGAL_NAME` | The registered company name (receipts); the drafts ask for it too |

The app never hard-codes these addresses: `GET /api/v1/config/capabilities` answers
`links.privacy_policy`, `links.terms_of_use` and `links.account_deletion`, each with an
`ar` and an `en` URL (`legal/links.py`), the same for every build. The same URLs go on
the App Store and Google Play listings (`docs/store/review_notes.md` §10).

## Account-deletion requests from the web

`/legal/delete-account/` queues a request (address and optional reason) in
**Account deletion requests** (`/dj-admin/legal/accountdeletionrequest/`), oldest
first. Nothing deletes an account automatically: write to the address to confirm the
requester owns it, erase the account through the existing erasure, then close the
request with **the account was erased**, **no account uses this address** or
**declined**. Requests go 180 days after they are handled.
