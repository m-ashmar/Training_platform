# Side bugs — found while building, fixed later

Anything noticed in passing that is **not** part of the task in hand goes here instead of
derailing it. One row per finding, newest at the top of its section. Fix them in a pass of
their own, then move the row to **Closed** with the commit that closed it.

Not for: work the current task owns, bugs the app session reports (that is
`BACKEND_BUGS.md`), or ideas — this is defects only.

| Field | Meaning |
|---|---|
| ID | `S-001`, in order |
| Where | `file.py:L42`, or the endpoint |
| What | one line: what is wrong, not what to do |
| Impact | who sees it go wrong |
| Sev | CRITICAL / HIGH / MEDIUM |
| Found | the build step that walked past it |

## Open

| ID | Where | What | Impact | Sev | Found |
|---|---|---|---|---|---|
| S-001 | `wallet/utils.py:24` `get_platform_revenue_wallet` | The platform's revenue wallet is ONE wallet, so it holds one currency for ever — the first payment decides it. A plan priced in the other currency is minted into the client's wallet, logged as an error and never moves on. | Only SYP is sold today (USD is legacy rows), so nobody is affected yet; the day a second currency is priced, its revenue sits in customers' wallets. | MEDIUM | step 4 |

## Closed

| ID | Where | What | Closed by |
|---|---|---|---|
