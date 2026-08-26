# EKTU — Halyk ePay payments

Paid access to Open edX courses through Halyk Bank's ePay gateway.

The bank's own API surface is confined to `halyk_payments/client.py`. Everything
else — pricing, enrolment, the pages the learner sees — does not change when the
production credentials arrive.

## Status

Written against the published contract at <https://epayment.kz/docs>: the token
request, the payment object, the postLink body, the status API and the response
codes are all taken from the documentation rather than guessed. Moving to the
university's own terminal is a config change and an image rebuild.

## How a purchase works

```
learner → /halyk/checkout/<course_id>/   creates a pending payment, shows the widget
        → Halyk payment page             card details never touch the university
bank    → /halyk/postlink/               server to server: confirms, then enrols
learner → /halyk/result/<invoice_id>/    only reports what the server recorded
```

Four rules are deliberate:

- **Only the callback grants access.** The browser coming back from the bank
  never enrols anybody, so a learner cannot open a paid course by visiting a
  URL.
- **The price is read from the course.** `start_checkout` takes the amount and
  the mode from `CourseMode`; nothing about the price comes from the request.
- **Every callback is checked twice.** Its `secret_hash`, amount, currency and
  terminal must match the payment, and then the bank's status API is asked
  again (`HALYK_VERIFY_WITH_STATUS_API`) before anyone is enrolled.
- **Uncertainty never becomes a failure.** A transaction still in progress, a
  non-final `reasonCode` or an unreachable bank leaves the payment pending for a
  human, because none of those are evidence that the learner did not pay.

Re-delivery of the same callback is harmless: the payment row is locked and the
enrolment happens only on the transition into the paid state.

## What the documentation pins down

| Thing | Where it lives |
| --- | --- |
| Token per operation, never cached, `secret_hash` returned on postLink | `client.get_payment_token` |
| Status-API token, which takes no `invoiceID`/`amount` | `client.get_api_token` |
| Payment object for `halyk.showPaymentWidget()`, `auth` = the whole token response | `views.checkout` |
| `code: "ok"` and `reasonCode: 0` on success | `views.postlink` |
| Non-final `reasonCode`s (454, 690, 3240 …) → ask again, do not fail | `client.RETRYABLE_REASON_CODES` |
| Loyalty bonuses settle part of an order (`amount_bonus`) | `client.total_paid` |
| `POST /operation/:id/charge`, `/cancel`, `/refund`, valid only from the right status | `client.charge_operation` and friends |
| `resultCode` 100 means the *request* worked; `statusName` says what happened to the money | `client.TransactionStatus` |
| Invoice numbers: 6–15 digits, unique also on the last six | `client.invoice_number` |
| `description` ≤ 125 bytes (exceeding it is `reasonCode` 3298) | `client.truncate_description` |
| `language` is `RUS`/`KAZ`/`ENG` | `views.LANGUAGES` |

### Either terminal scheme works

On a one-step (SMS) terminal — the default — the money is charged straight
away and `statusName` becomes `CHARGE`. On a two-step (DMS) terminal it stops
at `AUTH`, merely blocked on the card, which is not the same as being paid.
The bank's own sandbox terminal is two-step.

A hold is charged as soon as the transaction has been verified, so a two-step
terminal reaches `CHARGE` by itself and nothing needs configuring either way.
Order matters and is deliberate: the amount, currency and terminal are checked
*before* any money is taken, never after.

Setting `HALYK_AUTO_CAPTURE=false` leaves holds alone, and then no course opens
until someone captures them in the merchant portal. There is no reason to add
`AUTH` to `HALYK_ACCEPTED_STATUSES` any more; the app logs an error at startup
if it finds it there, because that opens courses for money nobody collected.

## Invoice numbers

ePay wants the number unique per order *and* unique across its last six digits.
Random numbers cannot promise that — twelve random digits start colliding on
their last six after a few thousand orders — so the number is
`HALYK_INVOICE_BASE + payment.pk`, which is monotonic. Raise the base if these
numbers must not collide with an older system's.

## Trying it against the bank's sandbox

The published sandbox terminal works today:

```bash
tutor config save \
  --set HALYK_ENABLED=true \
  --set HALYK_TEST_MODE=true \
  --set HALYK_CLIENT_ID=test \
  --set HALYK_CLIENT_SECRET=<the sandbox secret from epayment.kz/docs> \
  --set HALYK_TERMINAL_ID=67e34d63-102f-4bd1-898e-370781d0074d
```

The documentation's test cards pay successfully (`4405639704015096` 01/27 CVC
321, 3DS password `unlock`) or fail on purpose (`4003032704547597` 09/20 CVC
170), which is how the failure paths get exercised.

The sandbox secret is public, but it is still a credential and still belongs in
Tutor config rather than in this repository.

**The bank has to be able to call back.** `postLink` is built from the host the
learner is on, so on a local `tutor dev` the bank cannot reach it and the
payment stays pending forever — the money moves, the course does not open. Test
the sandbox on the public host, or pay locally and then settle it by hand:

```bash
tutor dev run lms ./manage.py lms halyk_reconcile
```

## Trying it with no bank at all

`HALYK_FAKE_GATEWAY` runs the whole flow offline: the checkout page posts a
callback shaped exactly like the bank's, and the learner is really enrolled.
The credentials are not used at all, so this is an alternative to the sandbox
rather than something to combine with it.

It refuses to start when `DEBUG` is off — leaving it on and running
`tutor local start` gets an `ImproperlyConfigured` at boot rather than a
platform that gives courses away. That is deliberate. Switch it back off
(`--set HALYK_FAKE_GATEWAY=false`) before going anywhere near production.

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

### Delete the free mode, or the price is decoration

Adding a price does not put a course behind a paywall. Open edX auto-enrols
anyone the moment a free `audit` or `honor` mode exists on the course, and it
does so *before* the learner ever sees a price — so a course with both modes is
free to anyone who clicks "Enroll now". Paid-only means `verified` is the only
mode on the course.

```bash
tutor local run lms ./manage.py lms halyk_courses
```

reports every course being sold and flags the ones still reachable for free or
priced in the wrong currency. It changes nothing; deleting a free mode is a
decision about what the university sells.

Open edX's own track-selection page is handled for you: `PaidCourseCheckoutMiddleware`
redirects it to this checkout for courses that are for sale. Left alone, that
page walks the learner through identity verification and never asks for money,
because upstream expects the ecommerce service to take the payment.

## Where this code lives, and why it is installed twice

This directory is a subdirectory of the edx-platform fork — it is not a separate
repository. One package, holding two things that run in two different places:

| Package | Entry point | Runs | Installed by |
| --- | --- | --- | --- |
| `tutor_ektu_halyk` | `tutor.plugin.v1` | on the host, next to `tutor` | you, once |
| `halyk_payments` | `lms.djangoapp` | inside the LMS container | the image build |

Both are discovered through entry points, so each has to be pip-installed into
the Python environment it runs in. The image build does the second one for you
from `HALYK_APP_SOURCE`. Only the first is manual.

`HALYK_APP_SOURCE` is a git URL rather than a path into the image, even though
the image is built from the very repository this directory lives in. The
install runs in Tutor's `python-requirements` build stage, and that stage has no
working tree — it binds the individual requirements files it needs and nothing
else — so `/openedx/edx-platform` does not exist there and a local path fails
the build with *"File ... does not exist"*.

## Installation

```bash
# 1. the Tutor plugin, on the host, into the same environment as tutor itself
pip install "git+https://github.com/boomboom0202/edx-platform@release/teak#subdirectory=ektu-halyk-payments"
tutor plugins enable ektu-halyk

# 2. configuration
tutor config save \
  --set HALYK_ENABLED=true \
  --set HALYK_TEST_MODE=true \
  --set HALYK_CLIENT_ID=... \
  --set HALYK_CLIENT_SECRET=... \
  --set HALYK_TERMINAL_ID=...

# 3. the app, into the image
tutor images build openedx
tutor local start -d
tutor local run lms ./manage.py lms migrate halyk_payments
```

`tutor plugins list` should show `ektu-halyk` as enabled before the build; if it
does not, the plugin landed in a different Python environment than `tutor` — the
usual cause — and the config above went nowhere. Compare `which tutor` with
`which pip`, or install with `$(dirname $(which tutor))/pip`.

That pip line clones the platform repository, which is large. If it is already
on the server, install from disk instead:

```bash
pip install -e /path/to/edx-platform/ektu-halyk-payments
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
| `HALYK_POSTLINK_IP_ALLOWLIST` | `[]` | Restrict the callback by source address. Empty means any. |
| `HALYK_VERIFY_WITH_STATUS_API` | `true` | Re-check with the bank before enrolling. Leave on. |
| `HALYK_ACCEPTED_STATUSES` | `["CHARGE"]` | Outcomes that grant access. |
| `HALYK_AUTO_CAPTURE` | `true` | Charge a hold on a two-step terminal instead of leaving it blocked. |
| `HALYK_INVOICE_BASE` | `1000000` | Added to the row id to form the invoice number. |
| `HALYK_COURSE_MODE` | `verified` | The mode a payment grants. |
| `HALYK_CURRENCY` | `KZT` | The only currency accepted. |
| `HALYK_APP_SOURCE` | this directory, from git | What pip installs during the build. |

Endpoint addresses and the OAuth scope are settings too, so they can be
corrected from Tutor config if the documentation changes.

## Operations

Payments are visible in Django admin under *Halyk payments*, searchable by
invoice, reference, username and email; the record is read-only because rows are
only ever created by the checkout flow.

Learners see their own at `/halyk/orders/`, which is where the "Order History"
entry in the user menu points. Upstream points that entry at an order-history
microfrontend belonging to the ecommerce service, which is not running here, so
the link was empty before.

A payment stuck in **pending** means the callback never resolved it — the bank
could not reach us, the server was restarting, or the transaction had not
finished when the callback arrived. The money may well have been taken, so
these have to be settled:

```bash
tutor local run lms ./manage.py lms halyk_reconcile
tutor local run lms ./manage.py lms halyk_reconcile --invoice 1000001
tutor local run lms ./manage.py lms halyk_reconcile --dry-run
tutor local run lms ./manage.py lms halyk_reconcile --include-failed
```

It asks the bank about each pending payment and enrols the ones it confirms,
through exactly the same check the callback uses — so it cannot open a course
that was not paid for, and it is safe to run from cron.

### Refunds and released holds

Refunding through the merchant portal moves the money but leaves the learner
enrolled and the payment recorded as paid, so the platform and the bank stop
agreeing with each other. Do it here instead:

```bash
tutor local run lms ./manage.py lms halyk_refund --invoice 1000004        # shows what it would do
tutor local run lms ./manage.py lms halyk_refund --invoice 1000004 --yes
tutor local run lms ./manage.py lms halyk_refund --invoice 1000004 --amount 20 --yes
```

A full refund withdraws access, because otherwise the learner keeps the course
and the money; a partial one leaves the enrolment alone. `--keep-access` and
`--withdraw-access` overrule that. The bank refuses refunds under ten tenge,
and only a charged transaction can be refunded at all.

On a two-step terminal a payment that was authorised but not accepted leaves
the learner's money blocked on their card, earning nobody anything. Release it:

```bash
tutor local run lms ./manage.py lms halyk_cancel --invoice 1000006 --yes
```

Both refuse to act without `--yes`, and both address the bank by its own
transaction id, which is recorded from the callback.

## Tests

```bash
pytest halyk_payments/tests/
```

They cover the things that would cost money if they broke: the price comes from
the course, a forged or mismatched callback enrols nobody, a repeated callback
enrols once, `AUTH` does not open a course, a non-final error code leaves the
payment pending, and a late failure notice does not revoke access already
granted.

## Not built yet

Saved cards, recurring payments and emailed receipts. Nothing depends on them.
