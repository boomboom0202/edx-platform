# EKTU — Halyk ePay payments

Paid access to Open edX courses through Halyk Bank's ePay gateway.

The bank's own API surface is confined to `halyk_payments/client.py`. Everything
else — pricing, enrolment, the pages the learner sees — is finished and does not
change when the credentials arrive.

## Status

Ready to run against the bank as soon as the terminal credentials exist. Four
assumptions about the bank's API are marked `CONTRACT` in `client.py` and
`views.py` and must be checked against <https://epayment.kz/docs> before taking
real money:

1. the fields of the token request,
2. the fields of the payment object handed to the widget,
3. the body of the `postLink` callback and the field carrying the outcome,
4. the path and shape of the status-check response.

If any of them differ, only those two files change.

## How a purchase works

```
learner → /halyk/checkout/<course_id>/   creates a pending payment, shows the widget
        → Halyk payment page             card details never touch the university
bank    → /halyk/postlink/               server to server: confirms, then enrols
learner → /halyk/result/<invoice_id>/    only reports what the server recorded
```

Two rules are deliberate:

- **Only the callback grants access.** The browser coming back from the bank
  never enrols anybody, so a learner cannot open a paid course by visiting a
  URL. Before enrolling, the callback is re-checked against the bank's status
  API (`HALYK_VERIFY_WITH_STATUS_API`), and a mismatched amount is refused.
- **The price is read from the course.** `start_checkout` takes the amount and
  the mode from `CourseMode`; nothing about the price comes from the request.

Re-delivery of the same callback is harmless: the payment row is locked and the
enrolment happens only on the transition into the paid state.

## Trying it before the credentials arrive

`HALYK_FAKE_GATEWAY` runs the whole flow with no bank involved: the checkout page
shows a button that posts a simulated callback, and the learner is really
enrolled. It refuses to start when `DEBUG` is off, so it cannot reach production.

```bash
tutor config save --set HALYK_ENABLED=true --set HALYK_FAKE_GATEWAY=true
tutor dev start -d
```

Then give a course a price and open
`/halyk/checkout/course-v1:ENV+HYD_01+2022/`.

## Setting a course price

The course needs a paid `CourseMode` in tenge. In Django admin, *Course Modes →
Course modes*:

| Field | Value |
| --- | --- |
| Course id | `course-v1:ENV+HYD_01+2022` |
| Mode slug | `verified` |
| Currency | `kzt` |
| Price | e.g. `50000` |

A course with no such mode simply is not for sale, and checkout refuses to
start. Note that Open edX defaults `currency` to `usd`; checkout refuses a
course priced in anything other than `HALYK_CURRENCY` rather than quietly
charging the number in tenge.

## Installation

```bash
tutor plugins enable ektu-halyk
tutor config save \
  --set HALYK_ENABLED=true \
  --set HALYK_TEST_MODE=true \
  --set HALYK_CLIENT_ID=... \
  --set HALYK_CLIENT_SECRET=... \
  --set HALYK_TERMINAL_ID=...
tutor images build openedx
tutor local start -d
tutor local run lms ./manage.py lms migrate halyk_payments
```

Going live: `--set HALYK_TEST_MODE=false` with the production credentials, then
rebuild.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `HALYK_ENABLED` | `false` | Master switch. Off means no payment pages at all. |
| `HALYK_TEST_MODE` | `true` | Use the bank's test endpoints. |
| `HALYK_FAKE_GATEWAY` | `false` | Skip the bank entirely. Requires `DEBUG`. |
| `HALYK_CLIENT_ID` | — | From the bank. |
| `HALYK_CLIENT_SECRET` | — | From the bank. Never commit it. |
| `HALYK_TERMINAL_ID` | — | From the bank. |
| `HALYK_POSTLINK_SECRET` | — | Shared secret on the callback, if the bank supports one. |
| `HALYK_POSTLINK_IP_ALLOWLIST` | `[]` | Restrict the callback by source address. Empty means any. |
| `HALYK_VERIFY_WITH_STATUS_API` | `true` | Re-check with the bank before enrolling. Leave on. |
| `HALYK_COURSE_MODE` | `verified` | The mode a payment grants. |
| `HALYK_CURRENCY` | `KZT` | The only currency accepted. |

Endpoint addresses are settings too, so they can be corrected from Tutor config
if the documentation says otherwise.

## Operations

Payments are visible in Django admin under *Halyk payments*, searchable by
invoice, reference, username and email; the record is read-only because rows are
only ever created by the checkout flow.

A payment stuck in **pending** means the bank never confirmed it. Nobody was
enrolled and, if money was actually taken, it needs a refund on the bank's side —
the plugin never issues refunds by itself.

## Tests

```bash
pytest halyk_payments/tests/test_services.py
```

They cover the parts that would cost money if they broke: the price comes from
the course, a repeated callback enrols once, and a late failure notice does not
revoke access that was already granted.

## Not built yet

Refunds, saved cards, recurring payments and receipts. The `Payment` model
records the bank's reference so refunds can be issued from the bank's portal in
the meantime.
