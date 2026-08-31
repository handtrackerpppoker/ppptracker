# PPPokerHA

PPPoker Hand Tracker — imports a PPPoker replay link, analyses the session, and
exports hands for PokerTracker / DriveHUD / GTO Wizard.

## Deploying

There is one path to production: **merge a PR into `main`.**

Pushing to `main` triggers both halves of a deploy:

- **Railway** builds and serves the app.
- **`.github/workflows/deploy-rules.yml`** publishes `firestore.rules`, but only
  when the rules (or the Firebase project config) actually changed.

Nothing else needs running by hand. `_push_and_deploy.bat` used to be a second,
parallel route that pushed straight to `main` and deployed the rules itself; it
is gone, because "which way did I ship it?" decided whether the security rules
went out, and that is not a question a deploy should ask.

Two things to remember when shipping:

- **Bump the asset cache busters** (`?v=N` on `style.css` / `app.js`) in every
  template whenever you touch `static/` — a service worker caches those files.
- **Verify prod actually serves the new build** rather than assuming the merge
  was enough: load the site and confirm the bumped `?v=N` appears.

To re-publish the rules without changing them — say someone edited them in the
Firebase console and you want the repo's version back — run the **Deploy
Firestore rules** workflow from the Actions tab.

If GitHub Actions itself is unavailable, the rules deploy is one command from a
checkout that has them (mind which branch you're on — the repo root usually sits
on `main`):

```bash
firebase deploy --only firestore:rules --project pppoker-analyser
```

### CI secrets

| Secret | Purpose |
| --- | --- |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Service account JSON used to publish `firestore.rules`. Needs the **Firebase Rules Admin** role on `pppoker-analyser`. |

## Running locally

```bash
pip install -r requirements.txt
```

Put the environment variables below in a `.env` at the repo root (loaded
automatically by `python-dotenv` when present), then:

```bash
python app.py
```

Tests are standalone scripts, run the same way CI does:

```bash
python test_tiering.py
```

## Internationalization (i18n)

The app uses [Flask-Babel](https://python-babel.github.io/flask-babel/) for
translated strings. Supported locales are listed in `app.config['LANGUAGES']`
in `app.py` (currently `en`, `pt_BR`).

### Adding a new translated string

Wrap the string in Jinja templates with `_('...')`, or `{% trans %}...{%
endtrans %}` for multi-line/block text:

```jinja
<span>{{ _('Points') }}</span>
```

In `app.py` (Python-side strings, e.g. flash messages), use the same `_('...')`
call — `flask_babel` provides it. `babel.cfg` lists which files get scanned
(`app.py` and everything under `templates/`); a string outside those won't be
picked up by extraction.

### How locale is chosen

`get_locale()` in `app.py` resolves the active locale in this order:

1. An explicit `lang` cookie (set by the language `<select>` in the header,
   see `static/app.js`) — a user's manual choice always wins.
2. The browser's `Accept-Language` header, best-matched against
   `app.config['LANGUAGES']`.
3. `en` as the final fallback.

This is intentionally **Accept-Language only — no GeoIP or other paid
geolocation/detection service** is used anywhere in the flow (per the pt-BR
scope Decision: zero-cost detection only).

### Adding a new locale

1. Add the locale code to `app.config['LANGUAGES']` in `app.py`.
2. Add an `<option>` for it to the `#lang-select` dropdown in
   `templates/index.html` (and any other page with the selector).
3. Generate a catalog for it and translate the extracted strings (see
   "Compiling translations" below):
   ```bash
   pybabel init -i translations/messages.pot -d translations -l <locale_code>
   ```
4. Fill in `msgstr` entries in the new
   `translations/<locale_code>/LC_MESSAGES/messages.po`, then compile.

### Compiling translations

After editing translatable strings or `.po` files, re-extract and rebuild the
compiled catalog:

```bash
pybabel extract -F babel.cfg -o translations/messages.pot .
pybabel update -i translations/messages.pot -d translations
# fill in any new/blank msgstr entries in translations/<locale>/LC_MESSAGES/messages.po
pybabel compile -d translations
```

`pybabel compile` must run before deploying — the app reads the compiled
`.mo` file, not the `.po` source.

### Poker taxonomy glossary (pt_BR)

The following terms are intentionally left **untranslated** in `pt_BR` —
Brazilian poker players use these English terms natively, matching how
PPPoker itself and other poker training tools present them. This list is
authoritative in the header comment of
`translations/pt_BR/LC_MESSAGES/messages.po`; keep the two in sync.

- Street names: Flop, Turn, River, Street
- Stack/format terms: Stack, BB / BBs, MTT, Satellite
- Hole-card jargon: hero, board, runout, all-in
- Stats: VPIP, PFR
- Tournament structure jargon: Showdown, Rebuy, Add-on

## Environment variables

Set these in Railway for the deployed app, and in `.env` locally.

### Firebase

| Variable | Required | Purpose |
| --- | --- | --- |
| `FIREBASE_API_KEY`, `FIREBASE_AUTH_DOMAIN`, `FIREBASE_PROJECT_ID`, `FIREBASE_STORAGE_BUCKET`, `FIREBASE_MESSAGING_SENDER_ID`, `FIREBASE_APP_ID`, `FIREBASE_MEASUREMENT_ID` | yes | Publishable client config, served to the browser by `/api/firebase-config`. |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | yes in prod | Admin SDK credentials as one JSON blob. Falls back to application-default credentials when unset. |

### Stripe

| Variable | Required | Purpose |
| --- | --- | --- |
| `STRIPE_SECRET_KEY` | yes | Server-side Stripe key. |
| `STRIPE_PRICE_ID`, `STRIPE_PRO_PRICE_ID`, `STRIPE_PROTEST_PRICE_ID` | yes | Subscription prices per plan. |
| `STRIPE_WEBHOOK_SECRET` | yes | Verifies `/api/stripe-webhook`, which is what flips `users/{uid}.is_pro`. |
| `STRIPE_EARLY_ACCESS_PRICE_LABEL`, `STRIPE_PRO_PRICE_LABEL` | no | Display copy for the pricing CTAs. |

### Tiered access

Added by the anon/free/pro tiering work. See
[docs/firestore-schema.md](docs/firestore-schema.md) for what each one guards.

| Variable | Required | Purpose |
| --- | --- | --- |
| `AD_TOKEN_SECRET` | yes | HMAC key for the single-use export unlock in the `X-Ad-Token` header. Without it `POST /api/ad-token` answers 503 and no token ever verifies, so gated exports fall back to spending a credit directly. |
| `ANON_SESSION_SECRET` | yes | HMAC key for the claim ticket a signed-out import returns. Without it signed-out imports still analyse, but cannot be claimed after signing in. |
| `CPX_APP_ID` | yes | CPX Research app id, sent to the browser so the survey widget can load. |
| `CPX_SECURE_HASH` | yes | CPX app secret. Verifies `POST /api/cpx/postback` (`md5(trans_id + secret)`) and derives the per-user `secure_hash`. Never sent to the browser. |
| `TALLY_SIGNING_SECRET` | no | Verifies `POST /api/tally/callback` (base64 HMAC-SHA256 of the raw body). Unset means no Tally submission is ever accepted. |
| `TALLY_FORM_URL` | no | The Tally form to embed when CPX has no eligible survey. Unset simply means no fallback is offered. |
| `GATE_STUB_MODAL_ENABLED` | no | Self-hosted "watch to unlock" modal that stands in for a real rewarded-video ad while ayeT-Studios/Wannads publisher approvals are pending (see `_showGateStubModal` in `static/app.js` and `POST /api/gate/stub-completion`). Defaults **on** — set to `0`/`false`/`no`/`off` to disable. Not yet wired to any gate check (that's a separate task); building this now lets the rest of the gating work ship without waiting on ad-network approval. |

Generate the two HMAC secrets with anything that produces 32+ random bytes:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Other

| Variable | Required | Purpose |
| --- | --- | --- |
| `APP_URL` | no | Origin used for Stripe success/cancel URLs when the request carries no `Origin`. |
| `PERMANENT_ADMIN_EMAILS` | no | Comma-separated emails that are always admin, so the admin page can't lock everyone out. Defaults to the project owner. |

## Provider setup

**CPX Research** — set the postback URL to `https://<host>/api/cpx/postback`. It
must carry `user_id`, `trans_id`, `hash`, `status` and `subid_1`; `subid_1` is
the unlock kind (`hand` or `tourney`) and is echoed back from the widget URL.

**Tally** — the form needs hidden fields named `uid` and `kind` (Tally populates
hidden fields from URL query params of the same name), and a webhook pointed at
`https://<host>/api/tally/callback` with signing enabled using
`TALLY_SIGNING_SECRET`.
# mirror test 2026-08-31T04:55:42Z
