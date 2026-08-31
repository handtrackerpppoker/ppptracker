# Firestore schema

Firestore is the source of truth for tiering. Every counter that decides what a
player may do lives here rather than in process memory, because gunicorn runs
several workers and a player's requests land on whichever one is free.

This document covers the documents the **tiered access** feature reads and
writes. Collections that predate it (`tournaments`, `config`, `gamification`,
`leaderboards`) are described only where tiering touches them.

Writers are named per field. Anything marked **server-only** is written through
the Admin SDK in `app.py` and is blocked for clients in `firestore.rules` — see
[Security rules](#security-rules).

---

## `users/{uid}`

One document per signed-in account.

| Field | Type | Written by | Notes |
| --- | --- | --- | --- |
| `uid` | string | client | Mirrors the document id. |
| `email` | string | client | Captured on sign-in; also what makes exports a login-gated action. |
| `first_seen` | timestamp | client | Server timestamp, set once on creation. |
| `last_seen` | timestamp | client | Server timestamp, refreshed on each sign-in. |
| `is_pro` | bool | **server-only** (Stripe webhook) | The whole Free/Pro split. `true` removes every quota, the history window and the survey gate. |
| `stripe_customer_id` | string | **server-only** (Stripe webhook) | Fallback lookup key when a subscription event carries no uid metadata. |
| `subscription_status` | string | **server-only** (Stripe webhook) | Stripe's raw subscription status (`active`, `past_due`, `canceled`, `unpaid`, …), written by `customer.subscription.updated`/`.deleted`. Observability only — `is_pro` is still the field that gates access; this just makes the expiration-demotion logic visible instead of trusted blindly. Unset for users who never had a Stripe subscription. |
| `last_payment_at` | int (epoch secs) | **server-only** (Stripe webhook) | Stamped on `checkout.session.completed` and `invoice.payment_succeeded` — the two events that represent an actual payment. Left untouched by `customer.subscription.updated` (status sync, not necessarily a new payment). Unset for users who have never paid (`app.py:stripe_webhook`). |
| `quota` | map | **server-only** | Today's usage counters. See below. |
| `credits` | map | **server-only** | Unspent survey unlocks. See below. |

### `quota`

Lazily created on the first import or export of the day. The whole map is
rewritten by `_bump_quota()` inside a transaction, which rolls the day over
first, so a stale `day` reads as zero without needing a nightly job.

| Key | Type | Notes |
| --- | --- | --- |
| `day` | string | UTC date, `YYYY-MM-DD`. A different value means every counter below is stale and reads as 0. |
| `imports` | int | Successful imports today. Free cap: `config/import_ads`' `free + gated` (default **1 + 2 = 3**, matching the old flat `FREE_IMPORTS_PER_DAY`); the first `free` need no unlock, the rest need a gate-stub-modal completion. Claiming a signed-out import counts as one. Enforced by `_import_gate()`. |
| `hand_exports` | int | Single-hand exports today. Free cap: **5/day** (`FREE_HAND_EXPORTS_PER_DAY`); the first **2** (`FREE_HAND_EXPORTS_UNGATED`) need no unlock, the rest need a gate-stub-modal completion (was CPX). Enforced by `_hand_export_gate()`. |
| `tourney_exports` | int | **No longer read by any gate check** — tourney-export enforcement moved to `users/{uid}/quota/tourney_export` (lifetime-free + per-week, see below). Left here only as a historical daily count. |

Pro accounts are never counted — no quota key is written for them at all.

### `credits`

Single-use unlocks earned by completing a survey or the gate-stub modal.
Deliberately **not** reset daily; capped instead, so a week of unlocks can't
be stockpiled and dumped.

| Key | Type | Cap | Notes |
| --- | --- | --- | --- |
| `survey_credit_hand` | int | 3 | Unlocks one gated single-hand export. Granted by a completed gate-stub-modal completion (`POST /api/gate/stub-completion`) — CPX no longer grants this one; see the gate-wiring task. |
| `survey_credit_tourney` | int | 1 | Unlocks one per-tournament export. Still granted by CPX (`GET/POST /api/cpx/postback`). |
| `survey_credit_import` | int | 3 | Unlocks one gated import. Granted by a completed gate-stub-modal completion, same as `survey_credit_hand`. |

Granted by `_grant_credit()` from a provider callback (CPX's postback, or the
gate-stub modal's completion endpoint); spent by `_consume_credit()` when the
export/import succeeds, or when traded for an ad token via `POST
/api/ad-token`. A reversal (CPX `status=2`) takes the credit back only while
it is still unspent.

Read/spent by `_import_gate()` (imports), `_hand_export_gate()` (hand
exports) and `_tourney_export_gate()` (tourney exports, once the lifetime
freebie is spent) — see the `_export_gate`/`_import_gate` section of `app.py`.

---

## `users/{uid}/quota/tourney_export` — server-only

A single document, keyed by a fixed id (`tourney_export`), holding the
tourney-export gating state. This is **separate from** the `quota` map field
on `users/{uid}` above — that map is today's daily hard/soft quota shape
(`imports`, `hand_exports`, `tourney_exports`) and it does not fit tourney
export's new rule, which is **lifetime, not daily**: 1 free export ever, then
1 per ISO week thereafter. Rather than force a lifetime+weekly rule into a
daily-reset shape, it gets its own doc.

| Field | Type | Notes |
| --- | --- | --- |
| `lifetime_free_used` | bool | Whether the one free-forever tourney export has been spent. Missing/absent reads as `False` — see backfill note below. |
| `lifetime_free_used_at` | timestamp \| null | When the lifetime freebie was spent. `null`/absent until then. |
| `current_week_iso` | string | ISO 8601 week of the last write, `'YYYY-Www'` (e.g. `'2026-W34'`), from Python's `datetime.isocalendar()` — **always resolved server-side**, never trusted from the client. |
| `current_week_used` | int | Exports counted against `current_week_iso`. A stored week that isn't the current one reads as `0` on the next read, the same lazy-rollover pattern `quota.day` uses — no nightly job needed. |
| `last_reset_at` | timestamp | When the counter was last written (bump or rollover). |

Read by `_tourney_export_state(uid)` (lazy, matches stored data against the
server-computed current week, never writes). Written by
`_bump_tourney_export_usage(uid)`, which spends the lifetime freebie first
and only starts incrementing `current_week_used` once `lifetime_free_used` is
`True`. Both are called from `_tourney_export_gate(req, uid)` — the tourney
branch of `_export_gate` — which decides whether to grant the lifetime
freebie, prompt a CPX survey for the weekly-gated slot, or block outright
once `current_week_used` reaches `config/export_ads.tourney_weekly_limit`.
The old daily `quota.tourney_exports` counter is no longer read by any gate
check; it is effectively dead going forward, though nothing deletes past
values already stored there.

**Backfill:** existing users have no `quota/tourney_export` doc at all.
`_tourney_export_state` treats a missing doc as `lifetime_free_used = False`
— i.e. every existing user gets one fresh free lifetime export the first time
the new model reads their state, rather than trying to infer "have they
already benefited from a free tourney export" from the old daily-counter
history (which can't actually answer that question — the old model gated
*every* tourney export behind a survey, so "have they exported before" says
nothing about whether they should get today's specific *lifetime freebie*).
`backfill_tourney_export_state.py` (one-shot, disposable — see the file
header) makes this explicit by writing `lifetime_free_used: False` onto every
user doc that doesn't already have the subdocument, so the state is visible
in Firestore immediately rather than only appearing lazily on first read.

---

## `users/{uid}/gate_events` — server-only

Append-only history of every gate check across the three actions that can
require an unlock: tourney export, hand export, and import. One document per
event, auto-generated id, never updated or deleted after being written — this
is what makes later reporting/audit possible without replaying quota state.
Shared shape by design: hand-export and import gates get history for free by
writing into the same subcollection instead of each inventing their own log.

| Field | Type | Notes |
| --- | --- | --- |
| `kind` | string | `'tourney_export'` \| `'hand_export'` \| `'import'`. |
| `gated` | bool | `True` when the action actually required an unlock to proceed; `False` when it went through free (inside a free allowance). |
| `gate_provider` | string \| null | Free-form, **not a fixed enum** — who granted the unlock. Values in use as of this writing: `'stub'` (the watch-to-unlock modal) and `'cpx'` (CPX Research survey). A future rewarded-video SDK adds `'ayet'` and/or `'wannads'` without any schema change here. `null` when `gated` is `False`, or when no fresh provider event applies (e.g. a previously-banked credit). |
| `at` | timestamp | When the event was recorded. |
| `gate_completion_id` | string \| null | The provider's own transaction/response id when it has one (e.g. CPX's `trans_id`), else `null`. |

Written by the single shared helper `_record_gate_event(uid, kind, gated,
provider, completion_id)` — best-effort, failures are logged and swallowed
rather than blocking the export/import they describe. Called from every
`_ExportGate.commit()` (via its `gate_kind`/`gated`/`provider` fields, set by
`_import_gate`, `_hand_export_gate` and `_tourney_export_gate`) once the
action it describes has actually succeeded, and from `cpx_postback()` /
`gate_stub_completion()` on a fresh (non-duplicate) provider completion.
Owner-readable like `ad_jtis` and `survey_completions`, for the same audit
reason.

The `'stub'` provider is written by `POST /api/gate/stub-completion` (the
"watch to unlock" modal), keyed by a client-generated `completion_id` via
`create()` so a double-clicked OK button or a retried request is recognised
as a duplicate and does not double-grant. That endpoint also grants the
matching credit (`survey_credit_hand` for `kind='hand_export'`,
`survey_credit_import` for `kind='import'`) on a genuinely new completion —
the same "provider callback grants a credit, the gate spends it" shape
`cpx_postback()` uses for `'cpx'`. It refuses outright (503) when
`GATE_STUB_MODAL_ENABLED` is off, rather than granting a credit for an ad
that was never shown.

**Known gap (documented, not fixed):** the stub-completion endpoint does not
verify that the client's 30s countdown actually elapsed — it trusts the
browser. A technical user who calls the endpoint directly, or edits
`_GATE_STUB_SECONDS` in devtools, still gets a completion recorded and the
credit it grants. Acceptable for an MVP stub with no ad revenue on the line.

---

## `users/{uid}/tournaments/{tourney_id}`

One document per tournament the player has imported; the hands themselves live
in Cloud Storage at `storage_path`. Written by `_merge_tournament()`.

Tiering reads one field from it:

| Field | Type | Notes |
| --- | --- | --- |
| `earliest_ts` | int (epoch secs) | The history window compares against this. Free accounts see only `earliest_ts >= now - 7 days` (`FREE_HISTORY_DAYS`); a document with no `earliest_ts` is always shown, because "undated" is not evidence of "old". |

Nothing is ever deleted for being outside the window — the filter is applied on
read, so upgrading restores the full history immediately.

---

## `users/{uid}/ad_jtis/{jti}` — server-only

One document per **spent** export unlock. Written with `create()`, which is
atomic: a replayed `X-Ad-Token` loses the race and is refused.

| Field | Type | Notes |
| --- | --- | --- |
| `kind` | string | `hand` or `tourney` — the endpoint class the token was scoped to. |
| `exp` | int (epoch secs) | The token's own expiry, 5 minutes after issue. |
| `used_at` | int (epoch secs) | When it was redeemed. |

The document id is the token's `jti` (a uuid4 hex). Its existence *is* the
"already spent" record, which is why clients cannot delete it.

---

## `users/{uid}/survey_completions/{completion_id}` — server-only

One document per survey payout, keyed by the provider's own transaction id
(`trans_id` for CPX, `responseId` for Tally). Written with `create()`, so a
redelivered webhook is recognised as a duplicate and pays once.

| Field | Type | Notes |
| --- | --- | --- |
| `source` | string | `cpx` or `tally`. |
| `kind` | string | `hand` or `tourney` — which credit was granted. |
| `status` | string | CPX status as delivered: `1` complete, `2` reversal. Tally submissions are recorded as `1`. |
| `at` | int (epoch secs) | When we processed it. |
| `credit_granted` | bool | False when the grant was refused because the credit was already at its cap. |
| `credit_reversed` | bool | Set by a CPX reversal that successfully clawed the credit back. |
| `reversed_at` | int (epoch secs) | When that happened. |
| `trans_id` / `response_id` | string | Provider id, mirroring the document id. |
| `amount_local`, `amount_usd`, `offer_id`, `subid_1` | string | CPX payload, kept as delivered for revenue reconciliation. |
| `form_id` | string | Tally form the submission came from. |

---

## `config/export_ads` — server-only

Admin-configured export limits, set from Admin → Ad Campaigns → Export Ads.
Read fresh from Firestore on every call via `_export_ads_config()`, so an
admin's change applies to the next request with no redeploy. A missing doc or
read failure falls back to `_EXPORT_ADS_DEFAULTS` in `app.py`.

| Field | Type | Written by | Notes |
| --- | --- | --- | --- |
| `hand_hard_limit` | int | **server-only** (admin) | Daily cap on hand exports. `0` blocks the kind outright. Default **5** (`FREE_HAND_EXPORTS_PER_DAY`). Read by `_hand_export_gate()`. |
| `hand_soft_limit` | int | **server-only** (admin) | Of `hand_hard_limit`'s slots, how many (counted from the end) need a gate-stub-modal unlock (was CPX; switched by the gate-wiring task). Default **3**. |
| `tourney_hard_limit` | int | **legacy, unread** | Old daily hard cap for tourney exports. Default **1**. |
| `tourney_soft_limit` | int | **legacy, unread** | Old daily soft (survey-gated) cap for tourney exports. Default **1**. |
| `tourney_lifetime_free` | int | **server-only** (admin) | Tourney-export model: number of exports free for the *lifetime* of the account. Default **1**. Read by `_tourney_export_gate()`, though only as a boolean today — see the note in that function. |
| `tourney_weekly_limit` | int | **server-only** (admin) | Tourney-export model: exports allowed per ISO week once the lifetime-free allowance is used up. Default **1**. Read by `_tourney_export_gate()`. |
| `updated_at` | int (epoch secs) | **server-only** (admin) | Stamped on every admin save. |
| `updated_by` | string | **server-only** (admin) | uid of the admin who last saved. |

**`tourney_hard_limit` / `tourney_soft_limit` are the old daily hard/soft
pair and are no longer read by any gate check** — `_tourney_export_gate()`
(the tourney branch of `_export_gate`, called via `_export_gate(req, uid,
'tourney')`) is fully on the `tourney_lifetime_free` / `tourney_weekly_limit`
model now, checked against the per-user counters in
`users/{uid}/quota/tourney_export` (see that section above). The pair is
kept in `_EXPORT_ADS_DEFAULTS` and in the admin CRUD surface
(`admin_export_ads_config_set()`, the public `/api/export-ads-config` GET,
and `static/app.js`'s admin-panel rendering of it) only because retiring
those is out of scope for the gate-wiring task — check that nothing there
still relies on them before deleting.

Written via `PATCH`-style `.set(doc, merge=True)` in
`admin_export_ads_config_set()` — a partial body only touches the fields it
names.

---

## `config/import_ads` — server-only

Admin-configured free/gated allowance for the daily import quota, set from
Admin → Ad Campaigns → Import Ads. Mirrors `config/export_ads` just above —
see `_import_ads_config()` / `_IMPORT_ADS_DEFAULTS` in `app.py`.

Read fresh from Firestore on every call via `_import_ads_config()`, so an
admin's change applies to the next request with no redeploy. A missing doc or
read failure falls back to `_IMPORT_ADS_DEFAULTS`.

| Field | Type | Written by | Notes |
| --- | --- | --- | --- |
| `free` | int | **server-only** (admin) | Imports per day that need no unlock. Default **1**. |
| `gated` | int | **server-only** (admin) | Additional gate-stub-modal-gated imports per day, on top of `free`. Default **2**. |
| `updated_at` | int (epoch secs) | **server-only** (admin) | Stamped on every admin save. |
| `updated_by` | string | **server-only** (admin) | uid of the admin who last saved. |

Read by `_import_gate(req, uid)`, called from `/api/analyze` and
`/api/analyze/claim` right before the import would actually be built/saved —
same "gate called last" convention `_export_gate` uses. It layers a
free/gated split on top of the *existing* daily `quota.imports` counter
(`FREE_IMPORTS_PER_DAY`'s old flat cutoff is now `free + gated`) rather than
introducing a second counter — see `_import_gate`'s docstring in `app.py`.

Written via `PATCH`-style `.set(doc, merge=True)` in
`admin_import_ads_config_set()` — a partial body only touches the fields it
names, never clobbering the other one or the `config/import_ads` document as a
whole.

---

## Storage: `anon_sessions/{token}.json`

Not Firestore, but part of the same flow. An import made while signed out is
analysed and parked here for **1 hour** instead of being persisted to any
account. The browser holds only an HMAC-signed token; possession of that token is
the entire authorisation to claim it.

```json
{ "player_uid": "<pppoker uid>", "records": [ /* raw hand records */ ] }
```

Blob metadata carries `created_at` (epoch secs). Expired objects are swept
best-effort on each new anonymous import (up to 100 per pass), and the blob is
deleted outright once claimed.

---

## Security rules

`firestore.rules` enforces the "server-only" column above. Two properties matter:

1. **`users/{uid}` update** may not touch `is_pro`, `stripe_customer_id`,
   `subscription_status`, `quota` or `credits`; **create** may not seed them
   either, so a delete-and-recreate cannot wash away a spent allowance.
2. **`ad_jtis`, `survey_completions`, `quota` and `gate_events` are excluded
   from the blanket subcollection grant**, not merely re-matched with a
   stricter rule. Rule matches are OR'd, so a permissive parent rule would
   outvote a strict child one, and the account that benefits from deleting a
   spent-unlock record (or its own gate-event history) is exactly the account
   that must not be able to.

All four subcollections stay owner-**readable**, so a player can audit their
own unlocks, payouts, and tourney-export/gate history.
