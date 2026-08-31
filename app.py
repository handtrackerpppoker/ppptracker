import os
import threading
import time
import traceback

# Load .env file when running locally (no-op if file absent or python-dotenv not installed)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
except ImportError:
    pass
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs

import requests
from flask import (Flask, g, has_app_context, jsonify, redirect, render_template,
                   request, send_file, send_from_directory, Response)
from flask_babel import Babel, gettext

from hand_parser import (process_hands, build_hand_rows, classify_game,
                         norm_room_name, CATEGORY_TOURNAMENT)
from hand_exporter import validate_hands, export_pokerstars
from tournament_analyzer import analyze_tournament
import gamification

app = Flask(__name__)

app.config['LANGUAGES'] = ['en', 'pt_BR']


def get_locale():
    # Explicit user choice (lang cookie) always wins over auto-detection.
    cookie_lang = request.cookies.get('lang')
    if cookie_lang in app.config['LANGUAGES']:
        return cookie_lang
    return request.accept_languages.best_match(app.config['LANGUAGES']) or 'en'


babel = Babel(app, locale_selector=get_locale)


@app.context_processor
def _inject_locale():
    return {'current_locale': get_locale()}


@app.after_request
def _set_coop_header(response):
    # unsafe-none lets Firebase's signInWithPopup check popup.closed without COOP violations
    if response.content_type and response.content_type.startswith('text/html'):
        response.headers['Cross-Origin-Opener-Policy'] = 'unsafe-none'
    return response

@app.errorhandler(Exception)
def _json_errors_for_api(exc):
    """An unhandled error under /api/ must still answer JSON.

    Flask's default 500 is an HTML page, which a fetch() caller parses as JSON
    and reports as `Unexpected token '<'` — the real cause never reaches the
    user or the browser console. Returning the exception text instead means a
    server-side bug shows up in the UI as what it actually is.

    For non-/api/ paths we defer to Flask's normal rendering. We must NOT
    re-raise here: because this handler is registered for the base Exception,
    a raised exception is re-caught by Flask's handle_exception, wrapped in a
    fresh InternalServerError, and handed straight back to this same handler —
    so `raise` turned every ordinary 404 (including the browser's automatic
    /favicon.ico request) into a 500. Returning the exception lets Flask render
    its standard page with the real status code; a non-HTTP error becomes a
    normal 500 page.
    """
    from werkzeug.exceptions import HTTPException, InternalServerError
    is_http = isinstance(exc, HTTPException)
    code = exc.code if is_http else 500
    if code >= 500:
        traceback.print_exc()
    if not request.path.startswith('/api/'):
        return exc if is_http else InternalServerError()
    return jsonify({'error': '%s: %s' % (type(exc).__name__, exc)}), code


REQUEST_TIMEOUT = 30
MAX_WORKERS = 10
REQUEST_DELAY = 0.1

_thread_local = threading.local()


def _session():
    if not hasattr(_thread_local, 'session'):
        _thread_local.session = requests.Session()
    return _thread_local.session


def _extract(url, key):
    qs = parse_qs(urlparse(url).query)
    vals = qs.get(key)
    return vals[0] if vals else None


def _rand():
    return f"1858_{time.time()}"


def _headers(referer):
    return {
        "User-Agent": "Mozilla/5.0",
        "Referer": referer,
        "Origin": "https://replay.pppoker.net",
        "Accept": "application/json, text/plain, */*",
    }


def _find_share_key(data):
    if isinstance(data, dict):
        for k in ("share_key", "shareKey", "sharekey", "L"):
            v = data.get(k)
            if isinstance(v, str) and len(v) > 20:
                return v
        for v in data.values():
            found = _find_share_key(v)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_share_key(item)
            if found:
                return found
    return None


def fetch_summaries(uid, rdkey, referer):
    r = _session().get(
        "https://api.pppoker.club/poker/api/get_hand_collection.php",
        params={"uid": uid, "rdkey": rdkey, "type": 0, "start_time": 0, "rand": _rand()},
        headers=_headers(referer),
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _fetch_share_key(uid, rdkey, gameid, referer):
    r = _session().get(
        "https://api.pppoker.club/poker/api/get_share_key.php",
        params={"uid": uid, "rdkey": rdkey, "gameid": gameid, "rand": _rand()},
        headers=_headers(referer),
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return _find_share_key(r.json())


def _fetch_full_hand(share_key, referer):
    r = _session().get(
        f"https://alicdn.pppoker.club/review_hand/{share_key}.json",
        headers={"User-Agent": "Mozilla/5.0", "Referer": referer,
                 "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _fetch_record(uid, rdkey, summary, referer):
    gameid = summary.get('D')
    if not gameid:
        return None
    try:
        time.sleep(REQUEST_DELAY)
        sk = _fetch_share_key(uid, rdkey, gameid, referer)
        if not sk:
            return None
        time.sleep(REQUEST_DELAY)
        fh = _fetch_full_hand(sk, referer)
        return {"summary": summary, "share_key": sk, "full_hand": fh}
    except Exception as exc:
        app.logger.warning("Failed hand %s: %s", gameid, exc)
        return None


@app.route("/")
def index():
    return render_template("index.html", gate_stub_modal_enabled=_GATE_STUB_MODAL_ENABLED)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    body = request.get_json(force=True, silent=True) or {}
    url  = body.get("url", "").strip()

    if not url:
        return jsonify({"error": "URL is required."}), 400

    # `uid` here is the PPPoker player id from the replay link — deliberately not
    # the Firebase uid, which is `viewer_uid` below.
    uid   = _extract(url, "uid")
    rdkey = _extract(url, "rdkey")
    if not uid or not rdkey:
        return jsonify({"error": "Invalid URL – could not find uid and rdkey parameters."}), 400

    claims     = _verify_bearer_claims(request)
    viewer_uid = claims.get('uid') if claims else None
    tier       = _tier(viewer_uid)

    gate = _import_gate(request, viewer_uid)
    if not gate.ok:
        return gate.error

    _EXPIRED_MSG = (
        "This link may have expired. Please re-open PPPoker, go to Hand History, "
        "and copy a fresh replay link."
    )

    try:
        summary_data = fetch_summaries(uid, rdkey, url)
    except Exception as exc:
        return jsonify({"error": f"Failed to fetch hand list: {exc}"}), 502

    # PPPoker returns code=0 on success; any other value means auth/expired.
    api_code = summary_data.get("code")
    if api_code is not None and api_code != 0:
        return jsonify({"error": _EXPIRED_MSG}), 200

    hands = (summary_data.get("I") or [])[:200]
    if not hands:
        # Empty hand list with code=0 can mean the rdkey silently rejected.
        return jsonify({"error": _EXPIRED_MSG}), 200

    records = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_record, uid, rdkey, s, url): s
            for s in hands
        }
        for future in as_completed(futures):
            rec = future.result()
            if rec:
                records.append(rec)

    # Newest first (matching original list order)
    records.sort(key=lambda r: r["summary"].get("C", 0), reverse=True)

    payload = _build_import_response(records, claims, tier, uid, len(hands), gate)

    if tier == 'anon':
        # Nothing is persisted for an anonymous import. Park it in Storage for an
        # hour so signing in can claim it, and ship the per-tournament graph data
        # inline — the detail endpoint that normally serves it needs an account.
        _sweep_anon_sessions()
        payload['session_token']      = _issue_anon_session(records, uid)
        payload['tournament_graphs']  = _tournament_graphs(records, payload['tournaments'])

    return jsonify(payload)


@app.route("/api/analyze/claim", methods=["POST"])
def analyze_claim():
    """Adopt a signed-out import into the signed-in account.

    The anonymous session holds the hands that were already fetched, so claiming
    costs no PPPoker round trip — but it is a real import: it counts against the
    day's quota and obeys the free tier's history window.
    """
    claims = _verify_bearer_claims(request)
    if not claims:
        return jsonify({'error': 'login_required'}), 401
    uid  = claims['uid']
    tier = _tier(uid)

    token = ((request.get_json(silent=True) or {}).get('session_token') or '').strip()
    if not token:
        return jsonify({'error': 'session_token is required'}), 400

    session = _load_anon_session(token)
    if not session:
        return jsonify({'error': 'session_expired'}), 404
    records = session.get('records') or []
    if not records:
        return jsonify({'error': 'session_expired'}), 404

    # Gated last, once the route knows there is actually something to claim —
    # same convention as _export_gate ("called last, once the route knows it
    # can actually produce the file"), so a stale/replayed token 404s as
    # session_expired instead of being misreported as gated. The blob is left
    # alone on a gate refusal, on purpose: the user can claim it tomorrow, or
    # after upgrading/unlocking, for as long as its hour lasts.
    gate = _import_gate(request, uid)
    if not gate.ok:
        return gate.error

    payload = _build_import_response(records, claims, tier,
                                     session.get('player_uid') or '', len(records), gate)
    if payload.get('saved'):
        _delete_anon_session(token)
    payload['claimed'] = bool(payload.get('saved'))
    return jsonify(payload)


def _build_import_response(records, claims, tier, player_uid, total_available, gate=None):
    """The shared body of /api/analyze and /api/analyze/claim.

    Prunes anything outside a free account's history window (from the response as
    well as from what gets persisted), saves, scores, and assembles the payload.

    gate is the _import_gate() result the caller already checked .ok on before
    doing any of this work; its commit() (bumping quota.imports and, if this
    import spent one, the credit/gate-event) only fires once we know the
    import actually saved something, same as every export route's gate.commit()
    only fires once the file was actually built. gate is optional (None) only
    for existing internal callers that don't go through a gate — none do
    today, but keeping it optional avoids a hard break if one shows up.
    """
    from hand_parser import extract_tourney_id as _extract_tid

    player_name = "Hero"
    for rec in records:
        for p in rec.get("full_hand", {}).get("info", {}).get("players", []):
            if p.get("isSelf"):
                player_name = p.get("user_name", "Hero")
                break
        if player_name != "Hero":
            break

    recent_hands, recent_won, stats, tournaments = process_hands(records)

    # Hands PPPoker listed but we could not retrieve, counted before the history
    # window prunes anything — otherwise a free account's pruned tournaments read
    # to the UI as a failed fetch.
    fetch_failed = max(0, total_available - len(records))

    # Free accounts keep a 7-day window. A tournament we can't date is kept —
    # "no timestamp" is not evidence that it is old.
    expired = 0
    if tier == 'free':
        cutoff = int(time.time()) - FREE_HISTORY_DAYS * 86400
        stale = {t.get('tourney_id') for t in tournaments
                 if t.get('earliest_ts') is not None and t['earliest_ts'] < cutoff}
        if stale:
            expired = len(stale)
            records = [r for r in records
                       if _extract_tid(r.get('summary', {}).get('D', '')) not in stale]
            recent_hands, recent_won, stats, tournaments = process_hands(records)

    validation = validate_hands(records)

    saved, new_ids = _save_tournaments(claims, records, tournaments)
    new_hands = len(new_ids)

    if saved and gate is not None:
        gate.commit()

    gamification_result = _score_import(claims, new_hands)

    # Compute stats/validation for only the truly-new records so the UI can
    # show "X new hands loaded" with accurate breakdown counts.
    if new_ids:
        new_recs = [r for r in records if r.get('summary', {}).get('D') in new_ids]
        _, _, new_stats, new_tourneys = process_hands(new_recs)
        new_validation = validate_hands(new_recs)
        new_tourney_count = len({_extract_tid(r.get('summary', {}).get('D', '')) for r in new_recs})
        _new_ts = [t.get('earliest_ts') for t in new_tourneys if t.get('earliest_ts')]
        new_ts_min = min(_new_ts) if _new_ts else None
        new_ts_max = max(_new_ts) if _new_ts else None
    else:
        new_stats = None
        new_validation = None
        new_tourney_count = 0
        new_ts_min = None
        new_ts_max = None

    return {
        "player": {"name": player_name, "uid": player_uid},
        "tier": tier,
        "total_fetched": len(records),
        "total_available": total_available,
        "fetch_failed": fetch_failed,
        "history_expired_tournaments": expired,
        "new_hands": new_hands,
        "new_tourney_count": new_tourney_count,
        "new_ts_min": new_ts_min,
        "new_ts_max": new_ts_max,
        "new_stats": new_stats,
        "new_validation": new_validation,
        "recent_hands": recent_hands,
        "recent_won_hands": recent_won,
        "stats": stats,
        "tournaments": tournaments,
        "validation": validation,
        "saved": saved,
        "gamification": gamification_result,
    }


def _tournament_graphs(records, tournaments):
    """Per-tournament graph payloads for an import that was never persisted."""
    from hand_parser import extract_tourney_id
    graphs = []
    for t in tournaments:
        tid = t.get('tourney_id')
        if not tid:
            continue
        recs = [r for r in records
                if extract_tourney_id(r.get('summary', {}).get('D', '')) == tid]
        if not recs:
            continue
        try:
            detail = _tournament_detail(recs, t)
        except Exception as exc:
            # One unconfigured tournament must not cost the whole import its graphs.
            print(f"[_tournament_graphs] {tid} skipped: {type(exc).__name__}: {exc}")
            continue
        detail['tourney_id'] = tid
        graphs.append(detail)
    return graphs


@app.route("/api/export/hand", methods=["POST"])
def export_hand():
    """Single hand from a persisted tournament (survey-gated past the free ones).

    The tournament id is required now: the process-global session cache this used
    to read is gone, so a hand is only exportable once it has been saved.
    """
    body = request.get_json(force=True, silent=True) or {}
    tid  = str(body.get("tourney_id") or "").strip()
    if not tid:
        return jsonify({"error": "tourney_id is required. Import and save this "
                                 "session before exporting."}), 400
    return export_persisted_hand(tid)


@app.route("/api/export/tournament", methods=["POST"])
def export_tournament():
    body = request.get_json(force=True, silent=True) or {}
    tid  = str(body.get("tourney_id") or "").strip()
    if not tid:
        return jsonify({"error": "Please provide a tourney_id."}), 400
    return export_persisted_tournament(tid)


@app.route("/api/export/pokerstars", methods=["POST"])
def export_ps():
    """Whole-session export — every hand of every tournament named in the body.

    Pro only: this is the bulk path, and it is the one the free tier upgrades for.
    """
    uid, err = _require_pro_export(request, 'full_session_export')
    if err:
        return err

    body     = request.get_json(force=True, silent=True) or {}
    platform = (body.get("platform") or "").strip()
    records, err = _collect_session_records(uid, body)
    if err:
        return err

    limit = body.get("limit")          # None = all hands
    if limit:
        records = records[:limit]
    try:
        filepath, _log = export_pokerstars(records, platform=platform,
                                           blind_levels_by_room=_blind_levels_by_room(records))
        return send_file(
            os.path.abspath(filepath),
            as_attachment=True,
            download_name=os.path.basename(filepath),
            mimetype="text/plain",
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _collect_session_records(uid, body):
    """Hands for every tourney_id in the body, newest first.

    Returns (records, None) or (None, error_tuple). Replaces the old in-process
    session cache: the client names the tournaments it just imported, and we read
    them back from that user's own persisted storage.
    """
    tids = body.get('tourney_ids')
    if not isinstance(tids, list):
        tids = [t for t in [str(body.get('tourney_id') or '').strip()] if t]
    tids = [str(t).strip() for t in tids if str(t).strip()]
    if not tids:
        return None, (jsonify({'error': 'tourney_ids is required. Import and save '
                                        'this session before exporting.'}), 400)

    records = []
    for tid in tids:
        recs, _doc = _fetch_tournament_records(uid, tid)
        if recs:
            records.extend(recs)
    if not records:
        return None, (jsonify({'error': 'No stored hands found for this session.'}), 404)
    records.sort(key=lambda r: r.get('summary', {}).get('C', 0), reverse=True)
    return records, None


# ── JSON export endpoints ─────────────────────────────────────────────────────

import re as _re
from datetime import datetime as _dt


@app.route("/api/export/json/all", methods=["POST"])
def export_json_all():
    """Raw JSON of the whole session — Pro only, same as the PokerStars variant."""
    uid, err = _require_pro_export(request, 'full_session_export')
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    records, err = _collect_session_records(uid, body)
    if err:
        return err
    import json as _json
    ts    = _dt.now().strftime("%Y%m%d_%H%M%S")
    filename = f"pppoker_full_export_{ts}.json"
    data = _json.dumps(records, indent=2)
    return Response(data, mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/api/export/json/tournament", methods=["POST"])
def export_json_tournament():
    body = request.get_json(force=True, silent=True) or {}
    tid  = str(body.get("tourney_id", "")).strip()
    if not tid:
        return jsonify({"error": "Please provide a tourney_id."}), 400
    return export_persisted_tournament_json(tid)


@app.route("/api/export/json/hand", methods=["POST"])
def export_json_hand():
    body = request.get_json(force=True, silent=True) or {}
    tid  = str(body.get("tourney_id") or "").strip()
    if not tid:
        return jsonify({"error": "tourney_id is required. Import and save this "
                                 "session before exporting."}), 400
    return export_persisted_hand_json(tid)


# ── Firebase config endpoint ─────────────────────────────────────────────────
# NOTE: these are publishable client-side keys (not secret), but we still serve
# them via env vars so the values are never committed to source control.
# Firestore security rules should restrict writes to documents where
#   request.resource.data.session_id == the document ID.

# ── Stripe + Firebase Admin ───────────────────────────────────────────────────

import stripe
import firebase_admin
from firebase_admin import credentials, firestore as admin_firestore, auth as admin_auth, storage as admin_storage

stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')
_STRIPE_PRICE_ID         = os.getenv('STRIPE_PRICE_ID', '')
_STRIPE_PROTEST_PRICE_ID = os.getenv('STRIPE_PROTEST_PRICE_ID', '')
_STRIPE_WEBHOOK_SEC      = os.getenv('STRIPE_WEBHOOK_SECRET', '')


def _get_admin_db():
    """Lazy-init Firebase Admin SDK and return a Firestore client."""
    if not firebase_admin._apps:
        sa_json = os.getenv('FIREBASE_SERVICE_ACCOUNT_JSON', '')
        if sa_json:
            import json
            cred = credentials.Certificate(json.loads(sa_json))
        else:
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    return admin_firestore.client()


def _get_admin_bucket():
    """Return the Firebase Storage bucket, initialising Admin SDK if needed."""
    _get_admin_db()
    bucket_name = os.getenv('FIREBASE_STORAGE_BUCKET', '')
    if not bucket_name:
        return None
    return admin_storage.bucket(name=bucket_name)


# ── Tiering: anon / free / pro ────────────────────────────────────────────────
# Three tiers, resolved per request from the bearer token:
#   anon — no bearer. Can import and look, can export nothing.
#   free — bearer, users/{uid}.is_pro falsy. Daily import/export quotas and a
#          7-day history window; the bigger exports need a survey credit.
#   pro  — bearer, is_pro true. No quota, no window, no survey gate.
# Firestore is the source of truth for every counter below; nothing is cached in
# process memory, because gunicorn runs several workers and a user's requests are
# spread across all of them.

FREE_HISTORY_DAYS         = 7
FREE_IMPORTS_PER_DAY      = 3
FREE_HAND_EXPORTS_PER_DAY = 5    # hard cap
FREE_HAND_EXPORTS_UNGATED = 2    # first N of the day need no survey credit
FREE_TOURNEY_EXPORTS_DAY  = 1

# Survey credits are single-use unlocks. They deliberately do NOT reset daily —
# but they are capped so a user can't stockpile a week of surveys and dump them.
# 'import' was added alongside the gate-stub wiring (Task 6): a completed stub
# modal grants one the same way a CPX postback grants a 'hand'/'tourney' one, so
# the import gate reuses the exact same grant/consume/ad-token machinery below
# instead of inventing a parallel one.
CREDIT_CAPS = {'hand': 3, 'tourney': 1, 'import': 3}
CREDIT_KINDS = tuple(CREDIT_CAPS)

# Two adjacent-but-different naming domains meet at the gate-events log:
# credit/quota code says 'hand' | 'tourney' | 'import'; gate_events (and the
# gate-stub-modal's own kind param) say 'hand_export' | 'tourney_export' |
# 'import'. This is the one mapping between them, used wherever a credit-kind
# needs to become a gate_events kind.
_CREDIT_KIND_TO_EVENT_KIND = {'hand': 'hand_export', 'tourney': 'tourney_export',
                              'import': 'import'}

_AD_TOKEN_SECRET      = os.getenv('AD_TOKEN_SECRET', '')
_ANON_SESSION_SECRET  = os.getenv('ANON_SESSION_SECRET', '')
_CPX_APP_ID           = os.getenv('CPX_APP_ID', '')
_CPX_SECURE_HASH      = os.getenv('CPX_SECURE_HASH', '')
_TALLY_SIGNING_SECRET = os.getenv('TALLY_SIGNING_SECRET', '')
# The Tally fallback needs a form to embed; the signing secret alone doesn't say
# which one. Optional — with it unset the client simply never offers the fallback.
_TALLY_FORM_URL       = os.getenv('TALLY_FORM_URL', '')

# Self-hosted "watch to unlock" modal that stands in for a real rewarded-video ad
# while ayeT-Studios/Wannads publisher approvals are pending. Default ON: unset
# means the stub renders. Only an explicit falsy value turns it off (e.g. once a
# real ad SDK is swapped in and the stub is no longer wanted at all).
_GATE_STUB_MODAL_ENABLED = os.getenv('GATE_STUB_MODAL_ENABLED', '1').strip().lower() \
    not in ('0', 'false', 'no', 'off')

# kind values the gate stub completion endpoint accepts — mirrors the import /
# hand-export soft-limit gates this modal will eventually stand in front of
# (Task 6, not part of this change).
_GATE_STUB_KINDS = ('import', 'hand_export')

_ANON_SESSION_TTL   = 3600          # 1h, matched by the signed token's exp
_ANON_SESSION_PREFIX = 'anon_sessions/'
_AD_TOKEN_TTL       = 300


def _utc_day(ts=None):
    from datetime import datetime as _d, timezone as _tz
    return _d.fromtimestamp(ts if ts is not None else time.time(),
                            tz=_tz.utc).strftime('%Y-%m-%d')


def _user_ref(uid):
    return _get_admin_db().collection('users').document(uid)


def _user_data(uid):
    """users/{uid} as a plain dict, memoised for the life of the request.

    Tier, quota, credits and the history window all live in this one document, so
    a single gated export would otherwise read it four times. A failed read
    answers {} — every caller's fallback for "no such user" is the same as its
    fallback for "couldn't ask", and both are the cautious side.
    """
    if not uid:
        return {}
    cache = None
    if has_app_context():
        cache = getattr(g, '_user_doc_cache', None)
        if cache is None:
            cache = g._user_doc_cache = {}
        if uid in cache:
            return cache[uid]
    try:
        snap = _user_ref(uid).get()
        data = (snap.to_dict() or {}) if snap.exists else {}
    except Exception as exc:
        print(f"[_user_data] read failed for uid={uid}: {type(exc).__name__}: {exc}")
        data = {}
    if cache is not None:
        cache[uid] = data
    return data


def _invalidate_user_data(uid):
    """Drop the memoised copy after a write, so a later read in the same request
    sees the new counters."""
    if has_app_context():
        cache = getattr(g, '_user_doc_cache', None)
        if cache:
            cache.pop(uid, None)


def _tier(uid):
    """'anon' | 'free' | 'pro' for a (possibly None) verified uid.

    A Firestore failure resolves to 'free' rather than 'pro': the quota path is
    the safe side to fail towards, and a Pro user briefly seeing a quota beats
    handing every free user the paid tier during an outage.
    """
    if not uid:
        return 'anon'
    return 'pro' if _user_data(uid).get('is_pro') else 'free'


_EMPTY_QUOTA = {'imports': 0, 'hand_exports': 0, 'tourney_exports': 0}


def _quota_state(uid):
    """Today's counters for uid: {day, imports, hand_exports, tourney_exports}.

    Read-only and lazy — a stored quota from a previous UTC day reads as all
    zeroes and is not rewritten until the next _bump_quota.
    """
    today = _utc_day()
    state = dict(_EMPTY_QUOTA, day=today)
    stored = _user_data(uid).get('quota')
    if isinstance(stored, dict) and stored.get('day') == today:
        for key in _EMPTY_QUOTA:
            state[key] = int(stored.get(key) or 0)
    return state


def _bump_quota(uid, key):
    """Transactionally +1 one of today's counters, rolling the day over first."""
    if key not in _EMPTY_QUOTA:
        raise ValueError(f'unknown quota key: {key}')
    from google.cloud import firestore as gcf
    db, ref, today = _get_admin_db(), _user_ref(uid), _utc_day()

    @gcf.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        stored = (snap.to_dict() or {}).get('quota') if snap.exists else None
        quota = (dict(_EMPTY_QUOTA, **{k: int(stored.get(k) or 0) for k in _EMPTY_QUOTA})
                 if isinstance(stored, dict) and stored.get('day') == today
                 else dict(_EMPTY_QUOTA))
        quota['day'] = today
        quota[key] = quota[key] + 1
        if snap.exists:
            transaction.update(ref, {'quota': quota})
        else:
            transaction.set(ref, {'quota': quota})
        return quota

    try:
        quota = _txn(db.transaction())
        _invalidate_user_data(uid)
        return quota
    except Exception as exc:
        # Never fail a completed import/export because the counter didn't stick.
        print(f"[_bump_quota] {key} failed for uid={uid}: {type(exc).__name__}: {exc}")
        return None


def _credits(uid):
    """{'hand': n, 'tourney': n} — unspent survey unlocks for uid."""
    out = {k: 0 for k in CREDIT_KINDS}
    stored = _user_data(uid).get('credits')
    if isinstance(stored, dict):
        for kind in CREDIT_KINDS:
            out[kind] = int(stored.get(f'survey_credit_{kind}') or 0)
    return out


def _grant_credit(uid, kind):
    """Transactionally +1 survey_credit_<kind>, capped at CREDIT_CAPS[kind].
    Returns True when the balance actually moved."""
    if kind not in CREDIT_CAPS:
        return False
    from google.cloud import firestore as gcf
    db, ref = _get_admin_db(), _user_ref(uid)
    field, cap = f'survey_credit_{kind}', CREDIT_CAPS[kind]

    @gcf.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        stored = ((snap.to_dict() or {}).get('credits') or {}) if snap.exists else {}
        current = int(stored.get(field) or 0)
        if current >= cap:
            return False
        credits = {f'survey_credit_{k}': int(stored.get(f'survey_credit_{k}') or 0)
                   for k in CREDIT_KINDS}
        credits[field] = current + 1
        if snap.exists:
            transaction.update(ref, {'credits': credits})
        else:
            transaction.set(ref, {'credits': credits})
        return True

    granted = _txn(db.transaction())
    _invalidate_user_data(uid)
    return granted


def _consume_credit(uid, kind):
    """Transactionally -1 survey_credit_<kind>. False (and no write) when zero."""
    if kind not in CREDIT_CAPS:
        return False
    from google.cloud import firestore as gcf
    db, ref = _get_admin_db(), _user_ref(uid)
    field = f'survey_credit_{kind}'

    @gcf.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        stored = ((snap.to_dict() or {}).get('credits') or {}) if snap.exists else {}
        current = int(stored.get(field) or 0)
        if current <= 0:
            return False
        credits = {f'survey_credit_{k}': int(stored.get(f'survey_credit_{k}') or 0)
                   for k in CREDIT_KINDS}
        credits[field] = current - 1
        transaction.update(ref, {'credits': credits})
        return True

    try:
        spent = _txn(db.transaction())
        _invalidate_user_data(uid)
        return spent
    except Exception as exc:
        print(f"[_consume_credit] {kind} failed for uid={uid}: {type(exc).__name__}: {exc}")
        return False


def _history_cutoff_ts(uid):
    """Oldest earliest_ts a free user may see, or None when there's no window."""
    return None if _tier(uid) == 'pro' else int(time.time()) - FREE_HISTORY_DAYS * 86400


def _is_expired(doc, cutoff):
    """True when a stored tournament falls outside the caller's history window.

    Nothing is ever deleted — Firestore keeps the whole history and upgrading
    brings it all straight back. A tournament with no earliest_ts is kept: not
    knowing when it happened is not evidence that it was long ago.
    """
    if cutoff is None or not doc:
        return False
    ts = doc.get('earliest_ts')
    return ts is not None and ts < cutoff


# ── Ad tokens: "this user has earned one unlock" ───────────────────────────────
# Issued only against a survey credit (see /api/ad-token), never on demand. The
# header contract is kept so the client always says "here is my unlock" the same
# way, whichever provider paid for it.

def _sign(secret, msg):
    import hmac, hashlib, base64
    digest = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip('=')


def _issue_ad_token(uid, kind):
    """(token, exp). Token is base64(uid|kind|exp|jti) plus an HMAC tag."""
    import base64, uuid
    exp = int(time.time()) + _AD_TOKEN_TTL
    payload = f'{uid}|{kind}|{exp}|{uuid.uuid4().hex}'
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')
    return f'{encoded}.{_sign(_AD_TOKEN_SECRET, encoded)}', exp


def _b64_decode(value):
    import base64
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)).decode()


def _verify_ad_token(header_val, uid, expected_kind):
    """True when header_val is a live, correctly-scoped, not-yet-spent unlock.

    Consumes the token on success by writing users/{uid}/ad_jtis/{jti} with
    create(), which is atomic — a replayed token loses the race and is rejected.
    """
    import hmac
    if not header_val or not _AD_TOKEN_SECRET:
        return False
    try:
        encoded, _, sig = header_val.strip().partition('.')
        if not encoded or not sig or not hmac.compare_digest(sig, _sign(_AD_TOKEN_SECRET, encoded)):
            return False
        tok_uid, kind, exp, jti = _b64_decode(encoded).split('|')
    except Exception:
        return False
    if tok_uid != uid or kind != expected_kind or int(exp) < int(time.time()):
        return False
    try:
        from google.api_core import exceptions as gexc
        try:
            _user_ref(uid).collection('ad_jtis').document(jti).create(
                {'kind': kind, 'exp': int(exp), 'used_at': int(time.time())})
        except gexc.AlreadyExists:
            return False       # replay
    except Exception as exc:
        # Can't prove single use — refuse rather than hand out a free unlock.
        print(f"[_verify_ad_token] jti write failed for uid={uid}: {type(exc).__name__}: {exc}")
        return False
    return True


# ── Export gate ───────────────────────────────────────────────────────────────

class _ExportGate:
    """Answer to "may this action be charged for?", plus what to charge.

    Shared by all three gates (import, hand export, tourney export) — the
    free/gated/hard-limit shapes differ, but "maybe consume a quota slot,
    maybe consume a credit, maybe bump the tourney counter, then log what
    happened" is the same commit-time contract for all of them.

    error is a ready-to-return Flask tuple when the answer is no. On yes, the
    route calls commit() once the action has actually succeeded, so a failed
    export/import doesn't burn the day's quota, the tourney counter, or the
    user's credit.

    gate_kind/gated/provider feed _record_gate_event (Task 4's audit log) —
    gate_kind is one of 'import' | 'hand_export' | 'tourney_export' (the
    gate_events kind domain, not _export_gate's own 'hand'/'tourney' kind
    argument). gated=False for a grant inside the free allowance; gated=True
    for one that spent a credit/ad-token. provider is only set for a *fresh*
    provider event — a banked credit being spent here carries no fresh
    provider, per docs/firestore-schema.md's note on gate_provider.
    """

    def __init__(self, uid, error=None, quota_key=None, credit_kind=None,
                 tourney_bump=False, gate_kind=None, gated=False, provider=None):
        self.uid = uid
        self.error = error
        self._quota_key = quota_key
        self._credit_kind = credit_kind
        self._tourney_bump = tourney_bump
        self._gate_kind = gate_kind
        self._gated = gated
        self._provider = provider

    @property
    def ok(self):
        return self.error is None

    def commit(self):
        if self._credit_kind:
            _consume_credit(self.uid, self._credit_kind)
        if self._quota_key:
            _bump_quota(self.uid, self._quota_key)
        if self._tourney_bump:
            _bump_tourney_export_usage(self.uid)
        if self._gate_kind:
            _record_gate_event(self.uid, self._gate_kind, self._gated,
                                provider=self._provider)


def _export_uid(req):
    """(uid, None) for a signed-in caller, (None, 401) otherwise.

    Every export needs an account now — that's the email capture, and it's also
    what makes the per-user quotas mean anything.
    """
    uid = _verify_bearer(req)
    if not uid:
        return None, (jsonify({'error': 'login_required'}), 401)
    return uid, None


_EXPORT_ADS_DEFAULTS = {
    # hard_limit = the daily cap (0 disables the kind entirely — no exports,
    # so the survey path is never reached). soft_limit = how many of those
    # hard_limit slots are survey-gated, counted from the *end*: the first
    # (hard_limit - soft_limit) exports are free, the rest need a survey.
    # soft_limit=0 therefore means "every slot up to the hard cap is free" —
    # no special-casing needed, it falls out of the arithmetic below.
    'hand_hard_limit':    FREE_HAND_EXPORTS_PER_DAY,                          # 5
    'hand_soft_limit':    FREE_HAND_EXPORTS_PER_DAY - FREE_HAND_EXPORTS_UNGATED,  # 3 gated
    # tourney_hard_limit/tourney_soft_limit are the OLD daily hard/soft pair.
    # As of the gate-wiring task, _tourney_export_gate() no longer reads
    # either of these — tourney-export enforcement is fully on the lifetime+
    # weekly model below now. They are kept here ONLY because they are still
    # part of this dict's persisted shape in Firestore (admin saves are
    # PATCH-style merges, so an old doc may still carry them) and because the
    # public /api/export-ads-config GET route + admin UI + static/app.js's
    # admin-panel rendering may still reference them (out of scope for this
    # task — see Task 7, running in parallel). Safe for a human to retire
    # from this dict, the admin config UI and that rendering code once that
    # overlap is checked and cleared.
    'tourney_hard_limit': FREE_TOURNEY_EXPORTS_DAY,                          # 1
    'tourney_soft_limit': FREE_TOURNEY_EXPORTS_DAY,   # today: the 1 slot is fully gated
    # Tourney-export admin config actually enforced now (see
    # _tourney_export_gate, which delegates to _tourney_export_state /
    # _bump_tourney_export_usage for the per-user lifetime+weekly counters):
    # 1 free-for-life export per user, then this many per ISO week after that.
    'tourney_lifetime_free': 1,
    'tourney_weekly_limit':  1,
}


def _export_ads_config():
    """Admin-configurable survey-gate limits for hand/tourney exports.

    Read fresh from Firestore on every call, the same as _active_plan() — an
    admin change takes effect on the very next request, no redeploy or cache to
    invalidate. Falls back to the defaults (which reproduce today's hardcoded
    behaviour) on a missing doc or any read failure.
    """
    cfg = dict(_EXPORT_ADS_DEFAULTS)
    try:
        snap = _get_admin_db().collection('config').document('export_ads').get()
        stored = snap.to_dict() if snap.exists else {}
    except Exception as exc:
        print(f"[_export_ads_config] read failed: {type(exc).__name__}: {exc}")
        stored = {}
    for key in _EXPORT_ADS_DEFAULTS:
        if key in stored:
            cfg[key] = stored[key]
    return cfg


_IMPORT_ADS_DEFAULTS = {
    # Imports aren't split into kinds the way exports are (hand vs tourney) —
    # just one daily allowance. 'free' is how many imports a day need no
    # unlock; 'gated' is how many more gate-stub-modal-gated imports are
    # available on top of those. Read by _import_gate() (below _export_gate).
    'free':  1,
    'gated': 2,
}


def _import_ads_config():
    """Admin-configurable free/gated import allowance.

    Mirrors _export_ads_config(): read fresh from Firestore on every call so
    an admin change takes effect on the very next request, no redeploy or
    cache to invalidate. Falls back to the defaults on a missing doc or any
    read failure.

    Read by _import_gate() (below _export_gate) to decide the free/gated
    split of the daily quota.imports counter.
    """
    cfg = dict(_IMPORT_ADS_DEFAULTS)
    try:
        snap = _get_admin_db().collection('config').document('import_ads').get()
        stored = snap.to_dict() if snap.exists else {}
    except Exception as exc:
        print(f"[_import_ads_config] read failed: {type(exc).__name__}: {exc}")
        stored = {}
    for key in _IMPORT_ADS_DEFAULTS:
        if key in stored:
            cfg[key] = stored[key]
    return cfg


def _export_gate(req, uid, kind):
    """Quota/survey gate for one export. kind is 'hand' or 'tourney'.

    Called last, once the route knows it can actually produce the file: a request
    that was going to 404 anyway must not be answered with "buy a survey first",
    and must not consume the day's allowance.

    pro → always allowed, uncounted, for both kinds.

    'hand' stays a daily free/gated/hard-limit shape (see _EXPORT_ADS_DEFAULTS'
    hand_hard_limit/hand_soft_limit) — the gated slots now unlock via a
    completed gate-stub-modal completion (which grants a 'hand' credit; see
    gate_stub_completion()) instead of CPX, but the credit/ad-token machinery
    that spends it is unchanged. Numbers are unchanged: 2 free / 3 gated / 5
    total per day.

    'tourney' is delegated to _tourney_export_gate — it no longer uses the
    daily quota shape at all (see that function).
    """
    if _tier(uid) == 'pro':
        return _ExportGate(uid)
    if kind == 'tourney':
        return _tourney_export_gate(req, uid)
    return _hand_export_gate(req, uid)


def _hand_export_gate(req, uid):
    """The 'hand' half of _export_gate, split out for clarity now that
    'tourney' has its own, differently-shaped gate function."""
    state = _quota_state(uid)
    ads = _export_ads_config()
    used, quota_key = state['hand_exports'], 'hand_exports'
    hard, soft = ads['hand_hard_limit'], ads['hand_soft_limit']

    if used >= hard:
        return _ExportGate(uid, error=(jsonify({
            'error': 'quota_exceeded', 'kind': 'hand', 'used': used, 'limit': hard,
            'upgrade': True}), 402))

    free_count = max(hard - soft, 0)
    if used < free_count:
        return _ExportGate(uid, quota_key=quota_key,
                            gate_kind='hand_export', gated=False)

    if _verify_ad_token(req.headers.get('X-Ad-Token', ''), uid, 'hand'):
        return _ExportGate(uid, quota_key=quota_key,
                            gate_kind='hand_export', gated=True)   # token already spent
    if _credits(uid).get('hand', 0) > 0:
        return _ExportGate(uid, quota_key=quota_key, credit_kind='hand',
                            gate_kind='hand_export', gated=True)

    return _ExportGate(uid, error=(jsonify({
        'error': 'survey_required', 'kind': 'hand', 'used': used, 'limit': hard}), 402))


def _require_pro_export(req, feature):
    """Gate for the exports that are Pro-only outright (no survey path).
    Returns (uid, None) when allowed, (None, error_tuple) when not."""
    uid = _verify_bearer(req)
    if not uid:
        return None, (jsonify({'error': 'login_required'}), 401)
    if _tier(uid) != 'pro':
        return None, (jsonify({'error': 'upgrade_required', 'feature': feature}), 403)
    return uid, None


def _import_gate(req, uid):
    """Free/gated/hard-limit gate for one import, same shape as
    _hand_export_gate but backed by _import_ads_config()'s free/gated fields
    instead of the export config, and by the existing quota.imports daily
    counter (unchanged — see _bump_quota/_quota_state) rather than a new
    counter of its own.

    uid may be falsy (an anonymous caller): anonymous imports were never
    counted against the daily quota before this gate existed (there is no
    uid to count them against), so a falsy uid bypasses the gate exactly the
    way a Pro uid does. Both routes that call this (/api/analyze,
    /api/analyze/claim) already only reach here after auth has been resolved
    to whatever uid (possibly None) applies.

    The gated slots unlock via a completed gate-stub-modal completion, which
    grants an 'import' credit (see gate_stub_completion()) — the same
    credit/ad-token machinery _hand_export_gate uses, just a different
    credit kind. There is no survey-provider path for imports; the stub
    modal is the only unlock mechanism (see AC6 in the wire-three-gates task
    for why: imports/hand-exports use the stub while ad-network approval is
    pending, tourney exports keep CPX).
    """
    if not uid or _tier(uid) == 'pro':
        return _ExportGate(uid)

    state = _quota_state(uid)
    ads = _import_ads_config()
    used = state['imports']
    hard = ads['free'] + ads['gated']

    if used >= hard:
        return _ExportGate(uid, error=(jsonify({
            'error': 'quota_exceeded', 'kind': 'import', 'used': used, 'limit': hard,
            'upgrade': True}), 402))

    if used < ads['free']:
        return _ExportGate(uid, quota_key='imports',
                            gate_kind='import', gated=False)

    if _verify_ad_token(req.headers.get('X-Ad-Token', ''), uid, 'import'):
        return _ExportGate(uid, quota_key='imports',
                            gate_kind='import', gated=True)   # token already spent
    if _credits(uid).get('import', 0) > 0:
        return _ExportGate(uid, quota_key='imports', credit_kind='import',
                            gate_kind='import', gated=True)

    return _ExportGate(uid, error=(jsonify({
        'error': 'survey_required', 'kind': 'import', 'used': used, 'limit': hard}), 402))


# ── Tourney-export: lifetime-free + weekly counter ─────────────────────────────
# The daily quota/credit shape above ("N/day, survey-gated") doesn't fit the new
# tourney-export model: 1 free EVER (lifetime, not daily), then 1/week. This is
# a separate counter, in its own users/{uid}/quota/tourney_export subcollection
# doc, resolved against the server's own clock — never a client-supplied week.
#
# Building the read/write helpers now; the actual gate check that decides
# *when* to block an export and prompt a rewarded-video/survey unlock is a
# later task. Nothing below is called from any route yet.

def _tourney_export_ref(uid):
    return _user_ref(uid).collection('quota').document('tourney_export')


def _current_iso_week(ts=None):
    """'YYYY-Www' for the given epoch seconds (default: now), UTC, ISO 8601
    week numbering (Python's own isocalendar() — Monday-start weeks, week 1 is
    the week containing the year's first Thursday). Always server-side; a
    client's timezone or clock is never consulted."""
    from datetime import datetime as _d, timezone as _tz
    dt = _d.fromtimestamp(ts if ts is not None else time.time(), tz=_tz.utc)
    iso_year, iso_week, _ = dt.isocalendar()
    return f'{iso_year}-W{iso_week:02d}'


_EMPTY_TOURNEY_EXPORT_STATE = {
    'lifetime_free_used': False,
    'lifetime_free_used_at': None,
    'current_week_iso': None,
    'current_week_used': 0,
    'last_reset_at': None,
}


def _tourney_export_state(uid):
    """{lifetime_free_used, lifetime_free_used_at, current_week_iso,
    current_week_used, last_reset_at} for uid.

    Read-only and lazy, the same shape as _quota_state: a stored week that
    isn't this ISO week reads current_week_used as 0 without anyone having to
    rewrite the doc first (that happens lazily, on the next bump). The current
    week is always resolved server-side.
    """
    this_week = _current_iso_week()
    state = dict(_EMPTY_TOURNEY_EXPORT_STATE, current_week_iso=this_week)
    try:
        snap = _tourney_export_ref(uid).get()
        stored = snap.to_dict() if snap.exists else None
    except Exception as exc:
        print(f"[_tourney_export_state] read failed for uid={uid}: {type(exc).__name__}: {exc}")
        stored = None
    if isinstance(stored, dict):
        state['lifetime_free_used'] = bool(stored.get('lifetime_free_used'))
        state['lifetime_free_used_at'] = stored.get('lifetime_free_used_at')
        state['last_reset_at'] = stored.get('last_reset_at')
        if stored.get('current_week_iso') == this_week:
            state['current_week_used'] = int(stored.get('current_week_used') or 0)
    return state


def _bump_tourney_export_usage(uid):
    """Transactionally record one tourney-export use: spends the lifetime
    freebie first if it hasn't been spent yet, otherwise +1 on this ISO
    week's counter (rolling the week over first, mirroring _quota_state's day
    rollover). Returns the new state dict, or None on failure.

    Not wired to any route yet — the future gate-check task calls this once
    it has decided the export should count.
    """
    if not uid:
        return None
    from google.cloud import firestore as gcf
    db, ref = _get_admin_db(), _tourney_export_ref(uid)
    this_week = _current_iso_week()

    @gcf.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        stored = snap.to_dict() if snap.exists else None
        stored = stored if isinstance(stored, dict) else {}
        now = gcf.SERVER_TIMESTAMP

        if not stored.get('lifetime_free_used'):
            new_state = {
                'lifetime_free_used': True,
                'lifetime_free_used_at': now,
                'current_week_iso': stored.get('current_week_iso') or this_week,
                'current_week_used': int(stored.get('current_week_used') or 0),
                'last_reset_at': stored.get('last_reset_at') or now,
            }
        else:
            week_used = (int(stored.get('current_week_used') or 0)
                         if stored.get('current_week_iso') == this_week else 0)
            new_state = {
                'lifetime_free_used': True,
                'lifetime_free_used_at': stored.get('lifetime_free_used_at'),
                'current_week_iso': this_week,
                'current_week_used': week_used + 1,
                'last_reset_at': now,
            }

        if snap.exists:
            transaction.update(ref, new_state)
        else:
            transaction.create(ref, new_state)
        return new_state

    try:
        return _txn(db.transaction())
    except Exception as exc:
        print(f"[_bump_tourney_export_usage] failed for uid={uid}: {type(exc).__name__}: {exc}")
        return None


def _tourney_export_gate(req, uid):
    """The 'tourney' gate: lifetime-free-once, then survey-gated once per ISO
    week (config/export_ads' tourney_lifetime_free / tourney_weekly_limit —
    tourney_lifetime_free is always 1 unlock spent lazily on first use, so only
    tourney_weekly_limit is actually read here as a limit to compare against).

    Caller (_export_gate) has already handled the Pro bypass, so this only
    runs for a free uid.

      1. lifetime freebie unspent → grant, and commit() spends it via
         _bump_tourney_export_usage (which itself decides lifetime vs weekly).
      2. lifetime spent, under this week's limit → same as _hand_export_gate's
         gated slot: an X-Ad-Token or a banked 'tourney' credit (granted by
         CPX's postback — tourney keeps CPX, unlike hand/import) unlocks it;
         commit() both spends the credit/token AND bumps the weekly counter.
      3. lifetime spent, at this week's limit → blocked; the client's next
         move is CPX, same as the pre-rewiring behaviour.

    Deliberately does NOT pre-check tourney_lifetime_free from config: the
    model is "1 free, ever", not "N free, ever" — a lifetime freebie count
    above 1 isn't a shape this per-user doc or _bump_tourney_export_usage
    supports today, so this reads it as a boolean the way
    _tourney_export_state's own field name (lifetime_free_used) implies.
    """
    state = _tourney_export_state(uid)
    weekly_limit = _export_ads_config()['tourney_weekly_limit']

    if not state['lifetime_free_used']:
        return _ExportGate(uid, tourney_bump=True,
                            gate_kind='tourney_export', gated=False)

    used = state['current_week_used']
    if used < weekly_limit:
        if _verify_ad_token(req.headers.get('X-Ad-Token', ''), uid, 'tourney'):
            return _ExportGate(uid, tourney_bump=True,
                                gate_kind='tourney_export', gated=True)
        if _credits(uid).get('tourney', 0) > 0:
            return _ExportGate(uid, tourney_bump=True, credit_kind='tourney',
                                gate_kind='tourney_export', gated=True)
        return _ExportGate(uid, error=(jsonify({
            'error': 'survey_required', 'kind': 'tourney',
            'used': used, 'limit': weekly_limit}), 402))

    return _ExportGate(uid, error=(jsonify({
        'error': 'quota_exceeded', 'kind': 'tourney', 'used': used,
        'limit': weekly_limit, 'upgrade': True}), 402))


# ── Gate-event history ──────────────────────────────────────────────────────────
# One append-only subcollection, shared by every gated action (tourney export,
# hand export, import), so they all get a history/audit trail "for free" instead
# of three bespoke logs. gate_provider is deliberately a free-form string, not a
# fixed enum: today's values are 'stub' (the watch-to-unlock modal) and 'cpx'
# (the existing CPX Research survey), and a future rewarded-video SDK adds
# 'ayet' / 'wannads' without touching this schema.

def _record_gate_event(uid, kind, gated, provider=None, completion_id=None, doc_id=None):
    """Append one row to users/{uid}/gate_events.

    kind:      'tourney_export' | 'hand_export' | 'import'
    gated:     True when the action actually required an unlock (survey, ad,
               stub modal, …) to proceed; False when it went through free.
    provider:  free-form string naming who granted the unlock ('stub', 'cpx',
               …), or None when gated is False / the unlock was a spent credit
               with no fresh provider event.
    completion_id: the provider's own transaction/response id, when there is
               one (e.g. CPX's trans_id), else None. Stored on the event but
               NOT used as the document id unless doc_id is also passed.
    doc_id:    explicit Firestore document id, for callers that need
               idempotency keyed by a client- or provider-supplied id (e.g.
               the gate-stub modal keys this by its completion_id so a
               double-clicked OK button can't double-grant). Defaults to a
               fresh random id, which makes the write fire-and-forget.

    Best-effort and append-only for the default (random-id) case — a failure
    here must never block the export/import it's describing, so it only logs
    and returns None. When doc_id is given, an AlreadyExists is the caller's
    own idempotency signal and is returned as False rather than swallowed;
    every other failure is still swallowed and logged.

    Returns True if a new event was written, False if doc_id already existed,
    None if the write failed for any other reason (or wasn't attempted).

    Only called today from gate_stub_completion(); the future gate-wiring
    task calls this from the tourney-export, hand-export and import gate
    paths too.
    """
    import uuid
    from google.api_core import exceptions as gexc
    from google.cloud import firestore as gcf
    event = {
        'kind': kind,
        'gated': bool(gated),
        'gate_provider': provider,
        'at': gcf.SERVER_TIMESTAMP,
        'gate_completion_id': completion_id,
    }
    ref = _user_ref(uid).collection('gate_events').document(doc_id or uuid.uuid4().hex)
    try:
        ref.create(event)
        return True
    except gexc.AlreadyExists:
        return False
    except Exception as exc:
        print(f"[_record_gate_event] failed for uid={uid} kind={kind}: {type(exc).__name__}: {exc}")
        return None


# ── Anonymous import sessions ─────────────────────────────────────────────────
# An anon import is analysed but never persisted to the user's history — it lives
# in Cloud Storage for an hour so that signing in can claim it. The client holds
# only a signed token, so possession of the token is the whole authorisation.

def _anon_blob(token):
    bucket = _get_admin_bucket()
    return bucket.blob(f'{_ANON_SESSION_PREFIX}{token}.json') if bucket else None


def _sweep_anon_sessions():
    """Best-effort GC of expired anon sessions. Never raises."""
    try:
        bucket = _get_admin_bucket()
        if not bucket:
            return
        cutoff = time.time() - _ANON_SESSION_TTL
        for blob in bucket.list_blobs(prefix=_ANON_SESSION_PREFIX, max_results=100):
            created = (blob.metadata or {}).get('created_at')
            try:
                created = float(created) if created is not None else blob.time_created.timestamp()
            except Exception:
                continue
            if created < cutoff:
                blob.delete()
    except Exception as exc:
        print(f"[_sweep_anon_sessions] skipped: {type(exc).__name__}: {exc}")


def _issue_anon_session(records, player_uid=''):
    """Park records in Storage and return a signed base64(token|exp), or None.

    The stored payload keeps the PPPoker uid alongside the hands so a later claim
    can rebuild the exact same response the anonymous import returned.
    """
    import base64, json as _jj, uuid
    if not _ANON_SESSION_SECRET:
        return None
    token = uuid.uuid4().hex
    blob = _anon_blob(token)
    if blob is None:
        return None
    try:
        blob.metadata = {'created_at': str(int(time.time()))}
        blob.upload_from_string(
            _jj.dumps({'player_uid': player_uid, 'records': records}),
            content_type='application/json')
    except Exception as exc:
        print(f"[_issue_anon_session] upload failed: {type(exc).__name__}: {exc}")
        return None
    exp = int(time.time()) + _ANON_SESSION_TTL
    encoded = base64.urlsafe_b64encode(f'{token}|{exp}'.encode()).decode().rstrip('=')
    return f'{encoded}.{_sign(_ANON_SESSION_SECRET, encoded)}'


def _parse_anon_session(session_token):
    """Signed token → storage token, or None when it's forged or expired."""
    import hmac
    if not session_token or not _ANON_SESSION_SECRET:
        return None
    try:
        encoded, _, sig = session_token.strip().partition('.')
        if not encoded or not sig or not hmac.compare_digest(sig, _sign(_ANON_SESSION_SECRET, encoded)):
            return None
        token, exp = _b64_decode(encoded).split('|')
    except Exception:
        return None
    if int(exp) < int(time.time()):
        return None
    return token


def _load_anon_session(session_token):
    """{'player_uid': str, 'records': [...]} for a valid token, else None."""
    import json as _jj
    token = _parse_anon_session(session_token)
    if not token:
        return None
    try:
        blob = _anon_blob(token)
        if blob is None or not blob.exists():
            return None
        payload = _jj.loads(blob.download_as_bytes())
    except Exception as exc:
        print(f"[_load_anon_session] read failed: {type(exc).__name__}: {exc}")
        return None
    return payload if isinstance(payload, dict) else None


def _delete_anon_session(session_token):
    token = _parse_anon_session(session_token)
    if not token:
        return
    try:
        blob = _anon_blob(token)
        if blob is not None:
            blob.delete()
    except Exception as exc:
        print(f"[_delete_anon_session] delete failed: {type(exc).__name__}: {exc}")


# ── Survey providers: CPX Research (primary) + Tally (fallback) ───────────────
# Completing a survey is how a free user earns an export unlock. Both providers
# call us server-to-server; both are idempotent on the provider's own id, because
# retries are normal and paying twice for one survey is not.

def _verify_cpx_hash(trans_id, provided_hash):
    """CPX signs each postback as md5(trans_id + "-" + secure_hash)."""
    import hashlib, hmac
    if not _CPX_SECURE_HASH or not trans_id or not provided_hash:
        return False
    expected = hashlib.md5(f'{trans_id}-{_CPX_SECURE_HASH}'.encode()).hexdigest()
    return hmac.compare_digest(expected, provided_hash.strip().lower())


def _verify_tally_signature(raw_body, header_val):
    """Tally signs the raw request body: base64(HMAC-SHA256(body, secret))."""
    import base64, hashlib, hmac
    if not _TALLY_SIGNING_SECRET or not header_val:
        return False
    digest = hmac.new(_TALLY_SIGNING_SECRET.encode(), raw_body or b'', hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), header_val.strip())


def _record_survey_completion(uid, doc_id, payload):
    """Write the completion record, or report that we've already seen it.

    Returns (created, existing_dict). create() is what makes this safe against
    two concurrent deliveries of the same postback.
    """
    from google.api_core import exceptions as gexc
    ref = _user_ref(uid).collection('survey_completions').document(doc_id)
    try:
        ref.create(dict(payload, at=int(time.time())))
        return True, None
    except gexc.AlreadyExists:
        snap = ref.get()
        return False, (snap.to_dict() if snap.exists else {})


@app.route('/api/survey-config', methods=['GET'])
def survey_config():
    """Everything the client needs to open a survey for the signed-in user.

    secure_hash is computed here because CPX's per-user hash is keyed with the
    app secret, which must never reach the browser.
    """
    import hashlib
    uid = _verify_bearer(request)
    if not uid:
        return jsonify({'error': 'login_required'}), 401
    cpx = {}
    if _CPX_APP_ID:
        cpx = {'app_id': _CPX_APP_ID, 'ext_user_id': uid}
        if _CPX_SECURE_HASH:
            cpx['secure_hash'] = hashlib.md5(f'{uid}-{_CPX_SECURE_HASH}'.encode()).hexdigest()
    return jsonify({'cpx': cpx, 'tally_form_url': _TALLY_FORM_URL,
                    'caps': CREDIT_CAPS})


@app.route('/api/credits', methods=['GET'])
def get_credits():
    """Unspent survey unlocks — polled by the client while a survey is open."""
    uid = _verify_bearer(request)
    if not uid:
        return jsonify({'error': 'login_required'}), 401
    return jsonify(_credits(uid))


@app.route('/api/ad-token', methods=['POST'])
def issue_ad_token():
    """Trade one survey credit for a short-lived, single-use export unlock.

    The credit is spent here rather than at export time, so a client that prefers
    the header contract can use it; clients that just retry the export get the
    same effect with one fewer round trip.
    """
    uid = _verify_bearer(request)
    if not uid:
        return jsonify({'error': 'login_required'}), 401
    if not _AD_TOKEN_SECRET:
        return jsonify({'error': 'ad_tokens_unavailable'}), 503
    kind = ((request.get_json(silent=True) or {}).get('kind') or '').strip()
    if kind not in CREDIT_KINDS:
        return jsonify({'error': f'kind must be one of {", ".join(CREDIT_KINDS)}'}), 400
    if not _consume_credit(uid, kind):
        return jsonify({'error': 'survey_required', 'kind': kind}), 402
    token, exp = _issue_ad_token(uid, kind)
    return jsonify({'token': token, 'exp': exp})


@app.route('/api/cpx/postback', methods=['GET', 'POST'])
def cpx_postback():
    """CPX Research server-to-server callback. Answers a literal `1` on success.

    status 1 = completed (grant), status 2 = reversal (claw back if unspent).
    subid_1 carries which unlock the user was chasing when the survey opened.
    """
    args     = request.args if request.args else (request.form or {})
    uid      = (args.get('user_id') or '').strip()
    trans_id = (args.get('trans_id') or '').strip()
    kind     = (args.get('subid_1') or 'hand').strip()
    status   = (args.get('status') or '1').strip()
    ev_type  = (args.get('type') or '').strip().lower()

    if not _verify_cpx_hash(trans_id, args.get('hash') or ''):
        return jsonify({'error': 'invalid_hash'}), 403
    if not uid or not trans_id:
        return jsonify({'error': 'user_id and trans_id are required'}), 400
    if kind not in CREDIT_KINDS:
        kind = 'hand'

    payload = {
        'source': 'cpx', 'trans_id': trans_id, 'kind': kind, 'status': status,
        'amount_local': args.get('amount_local'), 'amount_usd': args.get('amount_usd'),
        'offer_id': args.get('offer_id'), 'subid_1': args.get('subid_1'),
    }

    if status == '2':
        _reverse_survey_credit(uid, trans_id, payload)
        return Response('1', mimetype='text/plain')

    created, _existing = _record_survey_completion(uid, trans_id, payload)
    if not created:
        return Response('1', mimetype='text/plain')     # already paid out
    if ev_type in ('', 'complete'):
        granted = _grant_credit(uid, kind)
    else:
        granted = False
    _user_ref(uid).collection('survey_completions').document(trans_id).update(
        {'credit_granted': bool(granted)})
    if granted:
        # Completion outcome for the shared gate-events audit log (Task 4) —
        # mirrors what gate_stub_completion() does for the stub provider.
        # kind here is 'hand'/'tourney'/'import' (the credit-kind domain);
        # gate_events wants 'hand_export'/'tourney_export'/'import' instead.
        # CPX is only ever opened client-side for 'tourney' today, but this
        # endpoint is provider-generic, so map defensively rather than assume.
        _record_gate_event(uid, _CREDIT_KIND_TO_EVENT_KIND.get(kind, kind),
                            True, provider='cpx', completion_id=trans_id)
    return Response('1', mimetype='text/plain')


def _reverse_survey_credit(uid, doc_id, payload):
    """Best-effort claw-back of a credit whose survey was reversed.

    Only takes a credit back while it is still unspent — once the export has
    happened there is nothing to reverse, and driving the balance negative would
    silently cost the user their next legitimate survey.
    """
    try:
        ref = _user_ref(uid).collection('survey_completions').document(doc_id)
        snap = ref.get()
        if not snap.exists:
            _record_survey_completion(uid, doc_id, dict(payload, credit_granted=False))
            return
        d = snap.to_dict() or {}
        if not d.get('credit_granted') or d.get('credit_reversed'):
            return
        kind = d.get('kind', 'hand')
        if _consume_credit(uid, kind):
            ref.update({'credit_reversed': True, 'reversed_at': int(time.time())})
    except Exception as exc:
        print(f"[_reverse_survey_credit] failed for uid={uid} {doc_id}: "
              f"{type(exc).__name__}: {exc}")


# ── Gate stub modal ("watch to unlock") ────────────────────────────────────
# Self-hosted stand-in for a real rewarded-video ad while ayeT-Studios/Wannads
# publisher approvals are pending — see _showGateStubModal in static/app.js.
# Per the 2026-08-24 product-owner directive, this is not a temporary MVP
# shim: ayeT/Wannads is shelved indefinitely, so the stub modal is the real,
# current unlock mechanism for imports and hand exports. Tourney exports keep
# CPX (see _tourney_export_gate / cpx_postback).
#
# This endpoint records that the stub ran its course AND grants the credit
# _import_gate/_hand_export_gate actually check — the same 'grant a credit,
# let the gate spend it' shape CPX's postback uses for hand/tourney, just
# with 'stub' as the provider instead of 'cpx'. It does not verify the 30s
# elapsed client-side; a technical user who bypasses the JS timer still gets
# recorded as completed and granted the credit — acceptable for an MVP stub
# with no real ad revenue at stake (documented as a known gap in
# docs/firestore-schema.md).
#
# Recorded through the shared _record_gate_event() helper (above), keyed by
# the client's completion_id via its doc_id param so a double-clicked OK
# button or a retried request replays the same id and gets refused as a
# duplicate rather than recorded (and credited) twice — _grant_credit is only
# called when _record_gate_event proves this is a genuinely new completion.
#
# Gated on GATE_STUB_MODAL_ENABLED (server-side mirror of the client's own
# check before it even shows the modal): when the flag is off, this endpoint
# refuses rather than silently granting a credit for an ad that was never
# shown — the clean seam AC6 asks for, ready for a real rewarded-video SDK's
# server-to-server callback to take this route's place.

_GATE_STUB_KIND_TO_CREDIT = {'import': 'import', 'hand_export': 'hand'}


@app.route('/api/gate/stub-completion', methods=['POST'])
def gate_stub_completion():
    """POST /api/gate/stub-completion — records one stub-modal completion and
    grants the credit it unlocks.

    Idempotent on completion_id (client-generated once per modal open): a
    replayed completion_id is recorded as a duplicate and does not grant a
    second credit.
    """
    if not _GATE_STUB_MODAL_ENABLED:
        return jsonify({'error': 'gate_stub_disabled'}), 503

    uid = _verify_bearer(request)
    if not uid:
        return jsonify({'error': 'login_required'}), 401

    data = request.get_json(silent=True) or {}
    kind = (data.get('kind') or '').strip()
    if kind not in _GATE_STUB_KINDS:
        return jsonify({'error': f'kind must be one of {", ".join(_GATE_STUB_KINDS)}'}), 400
    completion_id = (data.get('completion_id') or '').strip()
    if not completion_id:
        return jsonify({'error': 'completion_id is required'}), 400

    written = _record_gate_event(uid, kind, gated=True, provider='stub',
                                  completion_id=completion_id, doc_id=completion_id)
    if written is None:
        # A real Firestore failure, not just a duplicate — _record_gate_event
        # swallows it for its fire-and-forget callers, but this endpoint's
        # entire job is recording the completion, so surface it rather than
        # claiming success on a write that didn't happen.
        return jsonify({'error': 'failed_to_record'}), 500

    if written:
        _grant_credit(uid, _GATE_STUB_KIND_TO_CREDIT[kind])

    return jsonify({'ok': True, 'kind': kind, 'completion_id': completion_id,
                     'already_recorded': written is False})


def _tally_field(fields, name):
    """Pull a hidden field out of a Tally submission by key or label."""
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        if (f.get('key') or '').lower() == name or (f.get('label') or '').lower() == name:
            value = f.get('value')
            return str(value).strip() if value is not None else ''
    return ''


@app.route('/api/tally/callback', methods=['POST'])
def tally_callback():
    """Tally webhook — the no-eligible-survey fallback. No revenue, but it keeps
    the user moving and the answers are product research."""
    raw = request.get_data()
    if not _verify_tally_signature(raw, request.headers.get('Tally-Signature', '')):
        return jsonify({'error': 'invalid_signature'}), 403

    body = request.get_json(silent=True) or {}
    data = body.get('data') or {}
    response_id = str(data.get('responseId') or '').strip()
    fields = data.get('fields') or []
    uid  = _tally_field(fields, 'uid')
    kind = (_tally_field(fields, 'kind') or 'hand').lower()
    if kind not in CREDIT_KINDS:
        kind = 'hand'
    if not uid or not response_id:
        return jsonify({'error': 'uid and responseId are required'}), 400

    created, _existing = _record_survey_completion(uid, response_id, {
        'source': 'tally', 'response_id': response_id, 'kind': kind,
        'form_id': body.get('data', {}).get('formId'), 'status': '1',
    })
    if not created:
        return jsonify({'ok': True, 'duplicate': True})
    granted = _grant_credit(uid, kind)
    _user_ref(uid).collection('survey_completions').document(response_id).update(
        {'credit_granted': bool(granted)})
    return jsonify({'ok': True, 'granted': bool(granted)})


def _merge_tournament(db, bucket, uid, tid, new_records):
    """
    Atomically merge new_records into the persisted tournament doc/blob for tid.
    Wrapped in a Firestore transaction so a concurrent import racing on the same
    tid is forced to retry from a fresh read rather than clobbering this write.
    """
    import json as _jj, time as _tt
    from google.cloud import firestore as gcf
    from hand_parser import _seq_num

    doc_ref      = db.collection('users').document(uid).collection('tournaments').document(tid)
    storage_path = f"tournaments/{uid}/{tid}.json"

    @gcf.transactional
    def _txn(transaction):
        snapshot = doc_ref.get(transaction=transaction)
        # Preserve the first-import timestamp across re-imports; new docs get "now".
        first_seen = (snapshot.to_dict() if snapshot.exists else {}).get('first_seen') \
            or int(_tt.time())
        merged_records = new_records
        # Default: all incoming IDs are new (no prior stored data for this tournament)
        new_ids = {r.get('summary', {}).get('D') for r in new_records}
        if snapshot.exists and bucket:
            old_path = snapshot.to_dict().get('storage_path', storage_path)
            blob = bucket.blob(old_path)
            if blob.exists():
                try:
                    old_records = _jj.loads(blob.download_as_bytes())
                    old_ids = {r.get('summary', {}).get('D') for r in old_records}
                    seen    = {r.get('summary', {}).get('D') for r in new_records}
                    new_ids = seen - old_ids
                    merged_records = new_records + [
                        r for r in old_records if r.get('summary', {}).get('D') not in seen
                    ]
                except Exception:
                    pass  # unreadable old blob — fall back to just the new hands

        # Sort by per-tournament sequence number (tie-free), not by concatenation order.
        merged_records.sort(
            key=lambda r: _seq_num(r.get('summary', {}).get('D', '')), reverse=True)

        _, _, _, recomputed = process_hands(merged_records)
        if not recomputed:
            return 0
        stat = recomputed[0]

        if bucket:
            bucket.blob(storage_path).upload_from_string(
                _jj.dumps(merged_records), content_type='application/json')

        transaction.set(doc_ref, {
            'tourney_id':    tid,
            'room_name':     stat.get('room_name', ''),
            'is_mtt':        stat.get('is_mtt', False),
            # Persisted for queries that read Firestore directly; readers should
            # still re-derive via _classify_doc so docs written before the
            # play-money split (or by an older deploy) are classified correctly.
            'is_play_money': stat.get('is_play_money', False),
            'category':      stat.get('category', ''),
            'hands':         stat.get('hands', 0),
            'net':           stat.get('net', 0),
            'first_chips':   stat.get('first_chips', 0),
            'last_chips':    stat.get('last_chips', 0),
            'finish_busted': stat.get('finish_busted', False),
            'duration_secs': stat.get('duration_secs'),
            'earliest_ts':   stat.get('earliest_ts'),
            'blind_min':     stat.get('blind_min', 0),
            'blind_max':     stat.get('blind_max', 0),
            'vpip_pct':      stat.get('vpip_pct', 0.0),
            'pfr_pct':       stat.get('pfr_pct', 0.0),
            'af':            stat.get('af', 0.0),
            'wtsd_pct':      stat.get('wtsd_pct', 0.0),
            'biggest_win':   stat.get('biggest_win', 0),
            'biggest_loss':  stat.get('biggest_loss', 0),
            'max_players':   stat.get('max_players', 0),
            'storage_path':  storage_path,
            'first_seen':    first_seen,
            'updated_at':    int(_tt.time()),
        })
        return new_ids

    return _txn(db.transaction())


def _save_tournaments(claims, records, tournaments):
    """
    Merge each tournament from this import into per-tournament persisted storage.
    On a re-import of the same tourney_id, hands are merged (de-duped by gameid)
    with any previously stored hands for that tournament and stats recomputed
    from the merged set. Returns (saved, new_game_ids): saved is True when any
    tournament was written and new_game_ids is the set of game IDs not previously
    stored. `claims` are the already-verified token claims, or None for anonymous
    callers — an anonymous import persists nothing.

    Persistence is deliberately NOT gated on is_pro. Every signed-in player's hands
    are stored so the gamification economy can count genuinely-new hands for them;
    the Free/Pro split is enforced at display and export time instead (the history
    window and the export quota), not at write time.
    """
    if not claims or not claims.get('uid') or not tournaments:
        return False, set()
    try:
        from hand_parser import extract_tourney_id

        db = _get_admin_db()  # ensures firebase_admin.initialize_app() has run
        uid = claims['uid']

        bucket = _get_admin_bucket()

        all_new_ids = set()
        for t in tournaments:
            tid = t.get('tourney_id')
            if not tid:
                continue
            new_records = [r for r in records
                            if extract_tourney_id(r.get('summary', {}).get('D', '')) == tid]
            if not new_records:
                continue
            ids = _merge_tournament(db, bucket, uid, tid, new_records)
            if ids:
                all_new_ids.update(ids)
        return True, all_new_ids
    except Exception as exc:
        import traceback
        print(f"[_save_tournaments] FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False, set()


def _score_import(claims, new_hands):
    """Award gamification points for one import. Returns the award payload, or None.

    Deliberately refuses to credit in two cases:

      * no verified token — anonymous imports persist nothing, so there is no
        trustworthy new-hand count to score, and
      * no Storage bucket configured — _merge_tournament treats every incoming game
        ID as new when it cannot read the previously-stored blob, so without the
        bucket the same replay link could be re-imported for points indefinitely.
        That is currently only a cosmetic counter; once points ride on it, it is a
        farming hole, so scoring stays off rather than paying out numbers we cannot
        stand behind.
    """
    if not claims or not claims.get('uid') or new_hands <= 0:
        return None
    try:
        if not _get_admin_bucket():
            print('[gamification] scoring skipped: FIREBASE_STORAGE_BUCKET is unset, '
                  'so new-hand counts cannot be trusted')
            return None
        return gamification.on_import(_get_admin_db(), claims['uid'], new_hands,
                                      claims=claims)
    except Exception as exc:
        # Scoring must never turn a successful hand import into a failed request.
        print(f"[_score_import] FAILED: {type(exc).__name__}: {exc}")
        return None


@app.route('/api/gamification', methods=['GET'])
def get_gamification():
    """Points / streak / rank for the signed-in player — drives the header banner."""
    uid = _verify_bearer(request)
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify(gamification.snapshot(_get_admin_db(), uid))


@app.route('/api/admin/gamification', methods=['GET'])
def admin_get_gamification():
    uid = _verify_bearer(request)
    if not uid or not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    db = _get_admin_db()
    status = gamification.week_status(db)
    status['timezones'] = list(gamification.ALLOWED_TIMEZONES)
    return jsonify(status)


@app.route('/api/admin/gamification', methods=['POST'])
def admin_set_gamification():
    """Set the zone every day/week boundary and time-window rule is evaluated in."""
    uid = _verify_bearer(request)
    if not uid or not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403

    tz = ((request.get_json(silent=True) or {}).get('timezone') or '').strip()
    if tz not in gamification.ALLOWED_TIMEZONES:
        return jsonify({'error': f'Unknown timezone: {tz or "(empty)"}'}), 400

    _get_admin_db().collection('config').document('gamification').set({
        'timezone':   tz,
        'updated_at': int(time.time()),
        'updated_by': uid,
    }, merge=True)
    # The resolver memoises for a minute; drop it so the change is visible immediately.
    gamification.invalidate_tz_cache()
    return jsonify({'timezone': tz})


@app.route('/api/admin/gamification/settle', methods=['POST'])
def admin_settle_gamification():
    """Force-settle a closed week's podium. Idempotent — settling twice pays once."""
    uid = _verify_bearer(request)
    if not uid or not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403

    db   = _get_admin_db()
    week = ((request.get_json(silent=True) or {}).get('week_key') or '').strip()
    if not week:
        return jsonify({'error': 'week_key is required'}), 400
    if week == gamification.week_key(int(time.time()), gamification.resolve_tz(db)):
        return jsonify({'error': 'That week is still open.'}), 400

    result = gamification.settle_week(db, week)
    if result is None:
        return jsonify({'week_key': week, 'already_settled': True})
    return jsonify(result)


@app.route('/api/create-checkout-session', methods=['POST'])
def create_checkout_session():
    data  = request.get_json(silent=True) or {}
    tier  = data.get('tier', 'pro')
    # The normal path bills whichever plan the admin console has active; the
    # 'protest' tier stays a fixed test price.
    price = (_STRIPE_PROTEST_PRICE_ID if tier == 'protest'
             else _pricing_plans()[_active_plan()]['price_id'])
    if not stripe.api_key or not price:
        return jsonify({'error': 'Stripe not configured'}), 503
    # uid/email come from the verified token, never the request body: the webhook
    # grants is_pro to whatever uid it finds in this session's metadata, so a
    # client-supplied uid would let a caller pay once and upgrade someone else.
    claims = _verify_bearer_claims(request)
    if not claims:
        return jsonify({'error': 'Sign in required'}), 401
    uid       = claims.get('uid', '')
    email     = claims.get('email', '')
    origin    = request.headers.get('Origin') or os.getenv('APP_URL') or request.host_url.rstrip('/')
    try:
        session = stripe.checkout.Session.create(
            mode               = 'subscription',
            line_items         = [{'price': price, 'quantity': 1}],
            success_url        = f'{origin}/?session_id={{CHECKOUT_SESSION_ID}}&upgraded=1',
            cancel_url         = f'{origin}/',
            customer_email     = email or None,
            metadata           = {'uid': uid},
            subscription_data  = {'metadata': {'uid': uid}},
        )
        return jsonify({'url': session.url})
    except stripe.StripeError as e:
        return jsonify({'error': str(e)}), 400


def _uid_for_customer(db, customer_id):
    """Look up Firestore uid by Stripe customer_id (fallback when metadata is absent)."""
    if not customer_id or not db:
        return None
    try:
        docs = db.collection('users').where('stripe_customer_id', '==', customer_id).limit(1).get()
        for doc in docs:
            return doc.id
    except Exception as exc:
        # Swallowed on purpose (the webhook has other ways to find the uid), but
        # log it — a silent failure here means a paid subscription silently
        # fails to grant is_pro.
        print(f"[_uid_for_customer] lookup failed for customer={customer_id}: "
              f"{type(exc).__name__}: {exc}")
    return None


@app.route('/api/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig     = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig, _STRIPE_WEBHOOK_SEC)
    except (ValueError, stripe.SignatureVerificationError):
        return jsonify({'error': 'Invalid signature'}), 400

    db  = _get_admin_db()
    t   = event['type']
    obj = event['data']['object']

    if t == 'checkout.session.completed':
        uid = obj.get('metadata', {}).get('uid', '')
        if uid:
            db.collection('users').document(uid).set(
                {
                    'is_pro':            True,
                    'stripe_customer_id': obj.get('customer', ''),
                    'last_payment_at':    int(time.time()),
                },
                merge=True
            )

    elif t in ('customer.subscription.deleted', 'customer.subscription.updated'):
        # Status sync only — not necessarily a new payment, so last_payment_at
        # is intentionally left untouched here.
        uid = (obj.get('metadata', {}).get('uid', '')
               or _uid_for_customer(db, obj.get('customer', '')))
        if uid:
            status = obj.get('status', '')
            # subscription_status is written unconditionally (raw Stripe status,
            # for admin visibility) even on statuses like 'past_due' or 'trialing'
            # that don't move is_pro either way below.
            updates = {'subscription_status': status}
            if t == 'customer.subscription.deleted' or status in ('canceled', 'unpaid'):
                updates['is_pro'] = False
            elif status == 'active':
                updates['is_pro'] = True
            db.collection('users').document(uid).set(updates, merge=True)

    elif t == 'invoice.payment_succeeded':
        if obj.get('subscription'):   # only subscription invoices, not one-off
            uid = _uid_for_customer(db, obj.get('customer', ''))
            if uid:
                db.collection('users').document(uid).set(
                    {'is_pro': True, 'last_payment_at': int(time.time())},
                    merge=True
                )

    return jsonify({'received': True})


def _verify_bearer_claims(req):
    """Verify the Authorization: Bearer <token> header. Returns decoded claims or None."""
    auth_hdr = req.headers.get('Authorization', '')
    if not auth_hdr.startswith('Bearer '):
        return None
    try:
        _get_admin_db()  # ensure Firebase Admin SDK is initialized before token verification
        return admin_auth.verify_id_token(auth_hdr[7:])
    except Exception as exc:
        # Log before swallowing: a Firestore/network outage and a genuinely bad
        # token both return None here, and without this line the outage looks
        # identical to "everyone is signed out" with nothing in the logs.
        print(f"[_verify_bearer] token verification failed: {type(exc).__name__}: {exc}")
        return None


def _verify_bearer(req):
    """Verify the Authorization: Bearer <token> header. Returns uid or None."""
    claims = _verify_bearer_claims(req)
    return claims.get('uid') if claims else None


# Accounts that are admin no matter what /config/admins.uids says, so the admin
# page can never lock everyone out of itself. The promote/demote endpoint refuses
# to strip these, and the admin page renders them as a ticked, disabled checkbox.
_PERMANENT_ADMIN_EMAILS = frozenset(
    e.strip().lower()
    for e in os.getenv('PERMANENT_ADMIN_EMAILS', 'caiohn@gmail.com').split(',')
    if e.strip()
)
_PERM_ADMIN_UID_CACHE = None


def _permanent_admin_uids():
    """Resolve _PERMANENT_ADMIN_EMAILS to uids via Firebase Auth (memoised).

    A permanent admin who has never signed in has no account yet, so an
    unresolved email is simply skipped — it starts working the moment they do.
    """
    global _PERM_ADMIN_UID_CACHE
    if _PERM_ADMIN_UID_CACHE is not None:
        return _PERM_ADMIN_UID_CACHE
    uids, complete = set(), True
    for email in _PERMANENT_ADMIN_EMAILS:
        try:
            _get_admin_db()  # ensure Firebase Admin SDK is initialized
            uids.add(admin_auth.get_user_by_email(email).uid)
        except admin_auth.UserNotFoundError:
            complete = False  # not registered yet — look again next time
        except Exception as exc:
            print(f"[_permanent_admin_uids] lookup failed for {email}: "
                  f"{type(exc).__name__}: {exc}")
            complete = False
            break
    # Only a full resolution is cached, so an account created after boot (or an
    # Auth outage) can't strand this process with a permanently empty set.
    if complete:
        _PERM_ADMIN_UID_CACHE = uids
    return uids


def _is_admin(uid):
    """True if uid is listed in /config/admins.uids (publicly readable doc)
    or belongs to a permanent admin."""
    if not uid:
        return False
    if uid in _permanent_admin_uids():
        return True
    try:
        snap = _get_admin_db().collection('config').document('admins').get()
        return snap.exists and uid in (snap.to_dict().get('uids') or [])
    except Exception as exc:
        # Same reasoning as _verify_bearer_claims: a Firestore outage would
        # otherwise silently 403 every admin action with no trace.
        print(f"[_is_admin] admin lookup failed for uid={uid}: {type(exc).__name__}: {exc}")
        return False


def _classify_doc(d):
    """Re-derive is_play_money/category for one persisted tournament doc.

    Classification is recomputed on every read rather than trusted from the
    stored fields, so tournaments imported before the play-money split — and
    any whose stored category came from older rules — are bucketed correctly
    with no backfill migration.
    """
    return classify_game(d.get('room_name') or '', d.get('is_mtt', False))


@app.route('/api/tournaments', methods=['GET'])
def list_tournaments():
    db  = _get_admin_db()  # ensures firebase_admin.initialize_app() has run
    uid = _verify_bearer(request)
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401

    # Free accounts see a rolling 7-day window. Nothing is deleted — the filter
    # is applied on read, so upgrading restores the full history instantly.
    cutoff = _history_cutoff_ts(uid)
    docs = db.collection('users').document(uid).collection('tournaments').get()
    tournaments, hidden = [], 0
    for doc in docs:
        d = doc.to_dict()
        if _is_expired(d, cutoff):
            hidden += 1
            continue
        d.pop('storage_path', None)  # internal detail, not needed by client
        d.update(_classify_doc(d))
        tournaments.append(d)

    return jsonify({'tournaments': tournaments,
                    'history_days': None if cutoff is None else FREE_HISTORY_DAYS,
                    'hidden_by_history_cap': hidden})


# ── Admin: tournament-config CRUD (writes via Admin SDK, gated to /config/admins) ─
_TOURNEY_FIELDS = {
    'name': str, 'type': str, 'is_pko': bool, 'is_mtt': bool, 'currency': str,
    'buy_in_total': float, 'buy_in_prize': float, 'buy_in_rake': float,
    'starting_chips': int, 'starting_time': 'strlist',
    'level_duration_min': int, 'level_duration_rebuy_min': int,
    'level_duration_ft_min': int, 'late_reg_level': int,
    'rebuy': bool, 'rebuy_type': str, 'rebuy_cost_multiplier': float,
    'rebuy_period_end_level': int, 'rebuy_bulk_options': list,
    'addon': bool, 'addon_cost_multiplier': float, 'addon_max_units': int,
    'addon_bulk_options': list,
    'player_min': int, 'player_max': int,
    'break_every_min': int, 'break_duration_min': int,
    'itm_h': float, 'end_h': float, 'ft_h': float, 'max_blinds': int,
    'active': bool,
}

# Numeric config fields that are meaningless (and downstream-corrupting) if negative.
_TOURNEY_NON_NEGATIVE = {
    'buy_in_total', 'buy_in_prize', 'buy_in_rake', 'starting_chips',
    'level_duration_min', 'level_duration_rebuy_min', 'level_duration_ft_min',
    'late_reg_level', 'rebuy_cost_multiplier', 'rebuy_period_end_level',
    'addon_cost_multiplier', 'addon_max_units', 'player_min', 'player_max',
    'break_every_min', 'break_duration_min', 'itm_h', 'end_h', 'ft_h',
    'max_blinds',
}


def _coerce_tourney_payload(body):
    """Coerce a client JSON body to typed, whitelisted tournament-config fields.
    Empty string / None -> None; unparseable values fall back to None.
    Returns (data, errors); errors is a list of human-readable validation
    messages for values that parsed but are out of range."""
    data, errors = {}, []
    for key, typ in _TOURNEY_FIELDS.items():
        if key not in body:
            continue
        v = body[key]
        if v is None or (isinstance(v, str) and v.strip() == ''):
            data[key] = None
            continue
        try:
            if typ is bool:
                data[key] = v if isinstance(v, bool) else str(v).strip().lower() in ('1', 'true', 'yes', 'on')
            elif typ is int:
                data[key] = int(float(v))
            elif typ is float:
                data[key] = float(v)
            elif typ is list:
                seq = v if isinstance(v, list) else str(v).split(',')
                data[key] = [int(float(x)) for x in seq if str(x).strip() != '']
            elif typ == 'strlist':
                seq = v if isinstance(v, list) else str(v).split(',')
                data[key] = [s for s in (str(x).strip() for x in seq) if s] or None
            else:
                data[key] = str(v).strip()
        except (ValueError, TypeError):
            data[key] = None
        # A negative buy-in or chip count parses fine as a number but is never a
        # real tournament, and these values feed ROI/bankroll maths downstream.
        if key in _TOURNEY_NON_NEGATIVE and isinstance(data.get(key), (int, float)) \
                and not isinstance(data[key], bool) and data[key] < 0:
            errors.append(f'{key} must be >= 0')
    return data, errors


@app.route('/api/admin/tournaments', methods=['POST'])
def admin_create_tournament():
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    body = request.get_json(silent=True) or {}
    data, errors = _coerce_tourney_payload(body)
    if errors:
        return jsonify({'error': '; '.join(errors)}), 400
    if not data.get('name'):
        return jsonify({'error': 'Name is required'}), 400
    raw_id = (body.get('id') or data['name'] or '').strip().lower()
    tid = _re.sub(r'[^a-z0-9]+', '_', raw_id).strip('_')
    if not tid:
        return jsonify({'error': 'Could not derive a valid id from name/id'}), 400
    db = _get_admin_db()
    ref = db.collection('tournaments').document(tid)
    if ref.get().exists:
        return jsonify({'error': f'Tournament "{tid}" already exists'}), 409
    data.setdefault('active', True)
    data.setdefault('currency', 'AUD')
    data['created_at'] = int(time.time())
    ref.set(data)
    return jsonify({'ok': True, 'id': tid})


@app.route('/api/admin/tournaments/<tid>', methods=['PUT', 'PATCH'])
def admin_update_tournament(tid):
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    body = request.get_json(silent=True) or {}
    data, errors = _coerce_tourney_payload(body)
    if errors:
        return jsonify({'error': '; '.join(errors)}), 400
    if not data:
        return jsonify({'error': 'No valid fields to update'}), 400
    db = _get_admin_db()
    ref = db.collection('tournaments').document(tid)
    if not ref.get().exists:
        return jsonify({'error': 'Tournament not found'}), 404
    data['updated_at'] = int(time.time())
    ref.update(data)  # merge — preserves blind_structure_extra / override, etc.
    return jsonify({'ok': True, 'id': tid})


@app.route('/api/admin/tournaments/stamp-new', methods=['POST'])
def admin_stamp_new_tournaments():
    """Stamp created_at (epoch secs) on any tournament config missing it.

    Tournaments added by the disposable seed script carry no created_at, so the
    tournaments-page "New" marker has nothing to key on. Calling this (admin-only,
    triggered when an admin loads the page) back-fills the timestamp the first time
    a doc is seen, which starts its review window. Docs that already have a
    created_at are left untouched. Returns the list of ids that were stamped."""
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    db = _get_admin_db()
    now = int(time.time())
    stamped = []
    for doc in db.collection('tournaments').get():
        if doc.to_dict().get('created_at') is None:
            doc.reference.update({'created_at': now})
            stamped.append(doc.id)
    return jsonify({'ok': True, 'stamped': stamped})


def _ms_to_secs(ms):
    """Firebase Auth metadata timestamps are epoch ms; the UI wants secs."""
    try:
        return int(ms) // 1000 if ms else None
    except (TypeError, ValueError):
        return None


def _fs_ts_to_secs(ts):
    """Firestore server timestamps arrive as tz-aware datetimes; the UI wants secs."""
    try:
        return int(ts.timestamp()) if ts else None
    except (AttributeError, TypeError, ValueError):
        return None


@app.route('/api/admin/users', methods=['GET'])
def admin_list_users():
    """Every registered Firebase Auth account, flagged with its admin status.

    Firebase Auth is the authoritative user list — the Firestore `users`
    collection only gets a doc once someone loads the home page, so it silently
    misses anyone who has only ever used /tournaments or /leaks. Those accounts
    fall back to the is_pro/first_seen/last_seen/exports_today defaults below.
    """
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403

    db = _get_admin_db()
    snap = db.collection('config').document('admins').get()
    allowlist = set(snap.to_dict().get('uids') or []) if snap.exists else set()
    permanent = _permanent_admin_uids()

    # One collection read for every users/{uid} doc, keyed by uid — cheaper than
    # a per-row lookup, and a miss (no doc yet) just means the defaults below.
    today = _utc_day()
    profiles = {}
    for doc in db.collection('users').stream():
        d = doc.to_dict() or {}
        quota = d.get('quota') if isinstance(d.get('quota'), dict) else {}
        exports_today = (int(quota.get('hand_exports') or 0) + int(quota.get('tourney_exports') or 0)) \
            if quota.get('day') == today else 0
        profiles[doc.id] = {
            'is_pro':              bool(d.get('is_pro')),
            'first_seen':          _fs_ts_to_secs(d.get('first_seen')),
            'last_seen':           _fs_ts_to_secs(d.get('last_seen')),
            'exports_today':       exports_today,
            'subscription_status': d.get('subscription_status') or None,
            'last_payment_at':     d.get('last_payment_at'),  # already epoch secs, not a Firestore timestamp
        }

    users = []
    try:
        page = admin_auth.list_users()
        while page:
            for u in page.users:
                meta = getattr(u, 'user_metadata', None)
                is_perm = u.uid in permanent
                profile = profiles.get(u.uid, {})
                users.append({
                    'uid':           u.uid,
                    'email':         u.email or '',
                    'is_admin':      is_perm or u.uid in allowlist,
                    'is_permanent':  is_perm,
                    'disabled':      bool(getattr(u, 'disabled', False)),
                    'created_at':    _ms_to_secs(getattr(meta, 'creation_timestamp', None)),
                    'last_sign_in':  _ms_to_secs(getattr(meta, 'last_sign_in_timestamp', None)),
                    'is_pro':              profile.get('is_pro', False),
                    'first_seen':          profile.get('first_seen'),
                    'last_seen':           profile.get('last_seen'),
                    'exports_today':       profile.get('exports_today', 0),
                    'subscription_status': profile.get('subscription_status'),
                    'last_payment_at':     profile.get('last_payment_at'),
                })
            page = page.get_next_page()
    except Exception as exc:
        print(f"[admin_list_users] list_users failed: {type(exc).__name__}: {exc}")
        return jsonify({'error': f'Could not list users: {exc}'}), 500

    # Admins first, then alphabetical — the people you manage sit at the top.
    users.sort(key=lambda u: (not u['is_admin'], (u['email'] or '~').lower()))
    return jsonify({'users': users})


@app.route('/api/admin/users/<target_uid>/admin', methods=['POST'])
def admin_set_user_admin(target_uid):
    """Add or remove target_uid from /config/admins.uids."""
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403

    body = request.get_json(silent=True) or {}
    make_admin = body.get('is_admin')
    if not isinstance(make_admin, bool):
        return jsonify({'error': 'is_admin must be true or false'}), 400

    try:
        target = admin_auth.get_user(target_uid)
    except admin_auth.UserNotFoundError:
        return jsonify({'error': 'No such user'}), 404
    except Exception as exc:
        print(f"[admin_set_user_admin] get_user failed for {target_uid}: "
              f"{type(exc).__name__}: {exc}")
        return jsonify({'error': f'Could not look up user: {exc}'}), 500

    if not make_admin and target_uid in _permanent_admin_uids():
        return jsonify({
            'error': f'{target.email or target_uid} is a permanent admin '
                     f'and cannot be removed.'
        }), 400

    from google.cloud import firestore as gcf
    ref = _get_admin_db().collection('config').document('admins')
    # ArrayUnion/ArrayRemove are applied server-side, so two admins editing the
    # list at once can't clobber each other — no transaction needed.
    op = gcf.ArrayUnion([target_uid]) if make_admin else gcf.ArrayRemove([target_uid])
    ref.set({'uids': op}, merge=True)  # merge=True also creates the doc if absent
    return jsonify({'ok': True, 'uid': target_uid, 'is_admin': make_admin})


@app.route('/api/admin/users/<target_uid>/pro', methods=['PATCH'])
def admin_set_user_pro(target_uid):
    """Manually grant or revoke Pro access on users/{uid}.is_pro.

    Manually-granted pro users have no stripe_customer_id, so Stripe webhook
    events (checkout, subscription updates) can never resolve to their uid via
    _uid_for_customer() and won't silently overwrite this.
    """
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403

    body = request.get_json(silent=True) or {}
    make_pro = body.get('is_pro')
    if not isinstance(make_pro, bool):
        return jsonify({'error': 'is_pro must be true or false'}), 400

    try:
        admin_auth.get_user(target_uid)
    except admin_auth.UserNotFoundError:
        return jsonify({'error': 'No such user'}), 404
    except Exception as exc:
        print(f"[admin_set_user_pro] get_user failed for {target_uid}: "
              f"{type(exc).__name__}: {exc}")
        return jsonify({'error': f'Could not look up user: {exc}'}), 500

    ref = _get_admin_db().collection('users').document(target_uid)
    # .update() merges without a full-document overwrite, per project convention —
    # but a user who has never loaded the home page has no users/{uid} doc yet
    # (see admin_list_users), so fall back to a merge-set rather than 500ing on
    # what is otherwise a perfectly valid manual grant.
    if ref.get().exists:
        ref.update({'is_pro': make_pro})
    else:
        ref.set({'is_pro': make_pro}, merge=True)
    return jsonify({'ok': True, 'uid': target_uid, 'is_pro': make_pro})


# ── Pricing plan ─────────────────────────────────────────────────────────────
# Which plan is on sale lives in /config/pricing.active_plan so it can be flipped
# from the admin console at launch without a deploy. Each plan's Stripe price and
# display price come from its own env vars, read per call so a Railway variable
# change lands on restart rather than needing a code edit.

_DEFAULT_PLAN = 'early_access'


# 'pro' is the full price; every other plan is a discount off it, which is what
# lets the pricing card strike the full price through.
_FULL_PRICE_PLAN = 'pro'


def _pricing_plans():
    """Plan key -> {label, price_label, price_id, price_env}."""
    return {
        'early_access': {
            'label':       'Early Access',
            'price_label': os.getenv('STRIPE_EARLY_ACCESS_PRICE_LABEL', 'A$7.99/mo'),
            'price_id':    os.getenv('STRIPE_PRICE_ID', ''),
            'price_env':   'STRIPE_PRICE_ID',
        },
        'pro': {
            'label':       'Pro',
            # Deliberately NOT STRIPE_PRO_LABEL — that variable holds the Stripe
            # product name ("Pro subscription"), which is not a price.
            'price_label': os.getenv('STRIPE_PRO_PRICE_LABEL', 'A$13.99/mo'),
            'price_id':    os.getenv('STRIPE_PRO_PRICE_ID', ''),
            'price_env':   'STRIPE_PRO_PRICE_ID',
        },
    }


def _active_plan():
    """Current plan key, falling back to the default on any read failure.

    Pricing copy renders on every page load, so a Firestore blip must degrade to
    the default plan rather than break the page.
    """
    try:
        snap = _get_admin_db().collection('config').document('pricing').get()
        key = (snap.to_dict() or {}).get('active_plan') if snap.exists else None
        if key in _pricing_plans():
            return key
    except Exception as exc:
        print(f"[_active_plan] pricing lookup failed: {type(exc).__name__}: {exc}")
    return _DEFAULT_PLAN


@app.route('/api/pricing', methods=['GET'])
def pricing_get():
    """Public: the plan the site is currently selling, for the pricing copy.

    Also carries the full price so the card can strike it through whenever the
    active plan is a discount off it. Every visible price on the site is driven
    from this one response — see test_pricing_refs.py, which fails if a price
    literal is reintroduced anywhere the switch can't reach.
    """
    plans = _pricing_plans()
    key   = _active_plan()
    plan  = plans[key]
    full  = plans[_FULL_PRICE_PLAN]
    return jsonify({
        'plan':                key,
        'label':               plan['label'],
        'price_label':         plan['price_label'],
        'regular_price_label': full['price_label'],
        'is_discounted':       key != _FULL_PRICE_PLAN,
    })


@app.route('/api/admin/pricing', methods=['GET'])
def admin_pricing_get():
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    plans = _pricing_plans()
    return jsonify({
        'active_plan': _active_plan(),
        'plans': [{
            'key':               key,
            'label':             p['label'],
            'price_label':       p['price_label'],
            'stripe_configured': bool(p['price_id']),
        } for key, p in plans.items()],
    })


@app.route('/api/admin/pricing', methods=['POST'])
def admin_pricing_set():
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    key   = (request.get_json(silent=True) or {}).get('plan')
    plans = _pricing_plans()
    if key not in plans:
        return jsonify({'error': f'Unknown plan "{key}"'}), 400
    # Activating a plan with no Stripe price would 503 every checkout, so refuse.
    if not plans[key]['price_id']:
        return jsonify({
            'error': f'{plans[key]["label"]} has no Stripe price — '
                     f'set {plans[key]["price_env"]} before activating it.'
        }), 400
    _get_admin_db().collection('config').document('pricing').set({
        'active_plan': key,
        'updated_at':  int(time.time()),
        'updated_by':  uid,
    }, merge=True)
    return jsonify({'ok': True, 'active_plan': key})


@app.route('/api/export-ads-config', methods=['GET'])
def export_ads_config_get():
    """Public: the live import/export limits and gate mechanisms, for the
    tier-comparison copy on the main page (see _applyExportAdsCopy in
    app.js). No admin gate — these numbers are shown to every visitor
    already, just not always accurately once an admin changes them here.

    This is a *reshaped* view of the flat admin config (see
    _export_ads_config()/_import_ads_config(), still used as-is by the
    /api/admin/* routes and by _export_gate()) — nested by feature, with a
    free-form `gate` string naming today's gate mechanism for each. `gate`
    is deliberately not an enum: a future Feature shipping real Rewarded
    Video will change these values (e.g. to 'ayet_rewarded_video') with no
    shape change here.

    hand_export block's shape is unchanged from the admin config (still
    hand_hard_limit/hand_soft_limit) — only its `gate` value is new.
    tourney_export is reshaped from the legacy tourney_hard_limit/
    tourney_soft_limit pair to the new lifetime_free/weekly_limit model
    (see _EXPORT_ADS_DEFAULTS). import is new, sourced from
    _import_ads_config().
    """
    export_cfg = _export_ads_config()
    import_cfg = _import_ads_config()
    return jsonify({
        'hand_export': {
            'hand_hard_limit': export_cfg['hand_hard_limit'],
            'hand_soft_limit': export_cfg['hand_soft_limit'],
            'gate': 'stub_modal',
        },
        'tourney_export': {
            'lifetime_free': export_cfg['tourney_lifetime_free'],
            'weekly_limit': export_cfg['tourney_weekly_limit'],
            'gate': 'cpx_survey',
        },
        'import': {
            'free': import_cfg['free'],
            'gated': import_cfg['gated'],
            'total': import_cfg['free'] + import_cfg['gated'],
            'cadence': 'daily',
            'gate': 'stub_modal',
        },
    })


@app.route('/api/admin/export-ads-config', methods=['GET'])
def admin_export_ads_config_get():
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify(_export_ads_config())


@app.route('/api/admin/export-ads-config', methods=['POST'])
def admin_export_ads_config_set():
    """Whole-config replace of the fields present in the body — merge='True'
    on the Firestore write, so an admin can change one field without resending
    the others, but each field present is validated on its own type."""
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'Malformed request body'}), 400

    update = {}
    for key in ('hand_hard_limit', 'hand_soft_limit',
                'tourney_hard_limit', 'tourney_soft_limit',
                'tourney_lifetime_free', 'tourney_weekly_limit'):
        if key in body:
            val = body[key]
            if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                return jsonify({'error': f'{key} must be a non-negative integer'}), 400
            update[key] = val
    if not update:
        return jsonify({'error': 'No recognised fields in request body'}), 400

    update['updated_at'] = int(time.time())
    update['updated_by'] = uid
    _get_admin_db().collection('config').document('export_ads').set(update, merge=True)
    return jsonify(_export_ads_config())


@app.route('/api/admin/import-ads-config', methods=['GET'])
def admin_import_ads_config_get():
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify(_import_ads_config())


@app.route('/api/admin/import-ads-config', methods=['POST'])
def admin_import_ads_config_set():
    """Whole-config replace of the fields present in the body — merge='True'
    on the Firestore write, so an admin can change one field without resending
    the other, but each field present is validated on its own type."""
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'Malformed request body'}), 400

    update = {}
    for key in ('free', 'gated'):
        if key in body:
            val = body[key]
            if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                return jsonify({'error': f'{key} must be a non-negative integer'}), 400
            update[key] = val
    if not update:
        return jsonify({'error': 'No recognised fields in request body'}), 400

    update['updated_at'] = int(time.time())
    update['updated_by'] = uid
    _get_admin_db().collection('config').document('import_ads').set(update, merge=True)
    return jsonify(_import_ads_config())


def _fetch_tournament_records(uid, tourney_id):
    """Returns (records, doc_dict) for a persisted tournament, or (None, None)."""
    db  = _get_admin_db()
    doc = db.collection('users').document(uid).collection('tournaments').document(tourney_id).get()
    if not doc.exists:
        return None, None
    d = doc.to_dict()
    storage_path = d.get('storage_path', '')
    bucket = _get_admin_bucket()
    if not bucket or not storage_path:
        return None, d
    blob = bucket.blob(storage_path)
    if not blob.exists():
        return None, d
    import json as _jj
    return _jj.loads(blob.download_as_bytes()), d


def _norm_room_name(s):
    """Strip platform emoji/punctuation so room names compare cleanly, e.g.
    "🌐 LUCKY DAY" (as stored on hand records) == "LUCKY DAY" (config doc name)."""
    return norm_room_name(s)


def _resolve_tournament_cfg(room_name):
    """
    Resolve the static config for a tournament instance, matched by room name.
    Per-tournament values win; missing scalars fall back to
    /config/tournament_defaults. The blind ladder is composed as base+extra
    (or the override ladder for LUCKY DAY / TEXAS), falling back to the canonical
    80-level ladder when no config doc matches. Returns a plain dict.

    Memoised per request: every call scans the whole /tournaments collection plus
    three config docs, and a single anonymous import resolves one config per
    tournament in the batch, which without this would be a dozen full scans.
    """
    room = _norm_room_name(room_name)
    cache = None
    if has_app_context():
        cache = getattr(g, '_tourney_cfg_cache', None)
        if cache is None:
            cache = g._tourney_cfg_cache = {}
        if room in cache:
            return cache[room]

    cfg = _resolve_tournament_cfg_uncached(room_name, room)
    if cache is not None:
        cache[room] = cfg
    return cfg


def _resolve_tournament_cfg_uncached(room_name, room):
    db = _get_admin_db()

    cfg_doc = {}
    if room:
        for snap in db.collection('tournaments').get():
            cd = snap.to_dict()
            if _norm_room_name(cd.get('name')) == room:
                cfg_doc = cd
                break

    def _config(name):
        snap = db.collection('config').document(name).get()
        return snap.to_dict() if snap.exists else {}

    defaults         = _config('tournament_defaults')
    base_levels      = _config('blind_structure_base').get('levels', [])
    canonical_levels = _config('blind_ladder_canonical').get('levels', [])

    if cfg_doc:
        extra = cfg_doc.get('blind_structure_extra') or []
        levels = (list(extra) if cfg_doc.get('blind_structure_override')
                  else list(base_levels) + list(extra))
    else:
        levels = canonical_levels

    graph_required = (
        'itm_h', 'end_h', 'ft_h', 'max_blinds',
        'late_reg_level', 'level_duration_min',
    )
    missing_graph_fields = [key for key in graph_required if cfg_doc.get(key) is None]
    if not levels:
        missing_graph_fields.append('blind_levels')
    graph_ready = bool(cfg_doc) and not missing_graph_fields and bool(levels)

    def pick(key):
        v = cfg_doc.get(key)
        return v if v is not None else defaults.get(key)

    return {
        'name':                     cfg_doc.get('name', room_name),
        'blind_levels':             levels,
        'itm_h':                    pick('itm_h'),
        'end_h':                    pick('end_h'),
        'ft_h':                     pick('ft_h'),
        'max_blinds':               pick('max_blinds'),
        'late_reg_level':           pick('late_reg_level'),
        'level_duration_min':       pick('level_duration_min'),
        'level_duration_rebuy_min': pick('level_duration_rebuy_min'),
        'level_duration_ft_min':    pick('level_duration_ft_min'),
        'starting_chips':           pick('starting_chips'),
        'rebuy_period_end_level':   pick('rebuy_period_end_level'),
        'graph_ready':              graph_ready,
        'graph_missing_fields':     missing_graph_fields,
        'graph_config_found':       bool(cfg_doc),
    }


def _blind_levels_by_room(records):
    """{normalized_room_name: blind_levels} for every distinct room among
    `records`, for hand_exporter's per-hand "Level" header — a session export
    (e.g. /api/export/pokerstars) can span several different tournaments."""
    rooms = {
        (r.get('full_hand', {}).get('info', {}).get('room', {}).get('room_name') or '')
        for r in records
    }
    rooms.discard('')
    return {
        _norm_room_name(room): _resolve_tournament_cfg(room).get('blind_levels')
        for room in rooms
    }


def _tournament_detail(records, doc):
    """{'hands': rows, 'meta': meta} for one tournament.

    Shared by /api/tournaments/<tid>/hands and by the inline graph payload an
    anonymous import receives, so both render from identical data.

    Resolves the tournament's static config from Firebase (per-tournament values
    with a canonical fallback) and runs the post-tournament analyser so the graphs
    get the ACTUAL level per hand plus rebuy/add-on spots. This may be slow on
    large tournaments — acceptable for now; it is a pure function designed to be
    reused later by an asynchronous Cowork skill.
    """
    meta = {k: (doc or {}).get(k) for k in
            ['room_name', 'earliest_ts', 'last_chips', 'first_chips',
             'finish_busted', 'max_players']}

    cfg = _resolve_tournament_cfg(meta.get('room_name') or '')
    analysis = analyze_tournament(records, cfg)

    for key in ('itm_h', 'end_h', 'ft_h', 'max_blinds', 'late_reg_level',
                'level_duration_min', 'level_duration_rebuy_min',
                'level_duration_ft_min', 'blind_levels', 'graph_ready',
                'graph_missing_fields', 'graph_config_found'):
        meta[key] = cfg.get(key)
    if not meta.get('graph_ready'):
        if not meta.get('graph_config_found'):
            meta['graph_warning'] = gettext(
                'Graph cannot be displayed because this tournament has no matching '
                'configuration in the tournaments table.'
            )
        else:
            fields = ', '.join(meta.get('graph_missing_fields') or [])
            meta['graph_warning'] = gettext(
                'Graph cannot be displayed because this tournament configuration '
                'is missing: %(fields)s.', fields=fields
            )
    meta['rebuys'] = analysis['rebuys']
    meta['addons'] = analysis['addons']
    meta['spots']  = analysis['spots']

    rows = build_hand_rows(records)
    hand_levels = analysis['hand_levels']
    meta['hand_levels'] = hand_levels
    chip_scale = analysis.get('scale', 1)
    for row in rows:
        row['level'] = hand_levels.get(row.get('hand_num'))
        if chip_scale != 1:
            if row.get('chip_stack') is not None:
                row['chip_stack'] = round(row['chip_stack'] / chip_scale)
            if row.get('profit') is not None:
                row['profit'] = round(row['profit'] / chip_scale)
            if row.get('big_blind') is not None:
                row['big_blind'] = round(row['big_blind'] / chip_scale)

    return {'hands': rows, 'meta': meta}


@app.route('/api/tournaments/<tourney_id>/hands', methods=['GET'])
def tournament_hands(tourney_id):
    """Per-hand display rows for one persisted tournament (Tournament Details)."""
    uid = _verify_bearer(request)
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401

    records, doc = _fetch_tournament_records(uid, tourney_id)
    if records is None:
        return jsonify({'error': 'Tournament data not available'}), 404

    cutoff = _history_cutoff_ts(uid)
    if _is_expired(doc, cutoff):
        return jsonify({'error': 'history_expired', 'upgrade': True}), 404

    return jsonify(_tournament_detail(records, doc))


def _records_in_window(uid, tourney_id):
    """Stored records for one tournament, if the caller's tier can still see it.
    Returns (records, doc, None) or (None, None, error_tuple)."""
    records, doc = _fetch_tournament_records(uid, tourney_id)
    if records is None:
        return None, None, (jsonify({'error': 'Tournament data not available'}), 404)
    if _is_expired(doc, _history_cutoff_ts(uid)):
        return None, None, (jsonify({'error': 'history_expired', 'upgrade': True}), 404)
    return records, doc, None


def _find_hand(records, hand_id):
    """One record by gameid, dashes ignored (that's how the UI copies them)."""
    return next(
        (r for r in records if r.get('summary', {}).get('D', '').replace('-', '') == hand_id),
        None,
    )


@app.route('/api/tournaments/<tourney_id>/export', methods=['POST'])
def export_persisted_tournament(tourney_id):
    uid, err = _export_uid(request)
    if err:
        return err

    records, _doc, err = _records_in_window(uid, tourney_id)
    if err:
        return err

    gate = _export_gate(request, uid, 'tourney')
    if not gate.ok:
        return gate.error

    body     = request.get_json(force=True, silent=True) or {}
    platform = (body.get('platform') or '').strip()
    try:
        filepath, _ = export_pokerstars(records, platform=platform,
                                         blind_levels_by_room=_blind_levels_by_room(records))
        gate.commit()
        return send_file(
            os.path.abspath(filepath),
            as_attachment=True,
            download_name=os.path.basename(filepath),
            mimetype='text/plain',
        )
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/tournaments/<tourney_id>/export/json', methods=['POST'])
def export_persisted_tournament_json(tourney_id):
    uid, err = _export_uid(request)
    if err:
        return err

    records, doc, err = _records_in_window(uid, tourney_id)
    if err:
        return err

    gate = _export_gate(request, uid, 'tourney')
    if not gate.ok:
        return gate.error

    import json as _jj
    room     = _re.sub(r'[^A-Za-z0-9]', '', (doc or {}).get('room_name', ''))[:24]
    filename = f"pppoker_{room}_{tourney_id}.json" if room else f"pppoker_tourney{tourney_id}.json"
    data = _jj.dumps(records, indent=2)
    gate.commit()
    return Response(data, mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


@app.route('/api/tournaments/<tourney_id>/export/hand', methods=['POST'])
def export_persisted_hand(tourney_id):
    _get_admin_db()  # ensure Firebase Admin SDK is initialized before token verification
    uid, err = _export_uid(request)
    if err:
        return err

    body        = request.get_json(force=True, silent=True) or {}
    raw_hand_id = (body.get('hand_id') or '').strip()
    hand_id     = raw_hand_id.replace('-', '')
    platform    = (body.get('platform') or '').strip()
    if not hand_id:
        return jsonify({'error': 'hand_id required'}), 400

    records, _doc, err = _records_in_window(uid, tourney_id)
    if err:
        return err

    match = _find_hand(records, hand_id)
    if not match:
        return jsonify({'error': f"Hand '{raw_hand_id}' not found."}), 404

    gate = _export_gate(request, uid, 'hand')
    if not gate.ok:
        return gate.error

    try:
        filepath, _ = export_pokerstars([match], platform=platform,
                                         blind_levels_by_room=_blind_levels_by_room([match]))
        gate.commit()
        return send_file(
            os.path.abspath(filepath),
            as_attachment=True,
            download_name=os.path.basename(filepath),
            mimetype='text/plain',
        )
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/tournaments/<tourney_id>/export/json/hand', methods=['POST'])
def export_persisted_hand_json(tourney_id):
    _get_admin_db()  # ensure Firebase Admin SDK is initialized before token verification
    uid, err = _export_uid(request)
    if err:
        return err

    body        = request.get_json(force=True, silent=True) or {}
    raw_hand_id = (body.get('hand_id') or '').strip()
    hand_id     = raw_hand_id.replace('-', '')
    if not hand_id:
        return jsonify({'error': 'hand_id required'}), 400

    records, _doc, err = _records_in_window(uid, tourney_id)
    if err:
        return err

    match = _find_hand(records, hand_id)
    if not match:
        return jsonify({'error': f"Hand '{raw_hand_id}' not found."}), 404

    gate = _export_gate(request, uid, 'hand')
    if not gate.ok:
        return gate.error

    import json as _jj
    filename = f"pppoker_hand_{raw_hand_id}.json"
    data = _jj.dumps(match, indent=2)
    gate.commit()
    return Response(data, mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


_FIREBASE_ENV_KEYS = [
    'FIREBASE_API_KEY',
    'FIREBASE_AUTH_DOMAIN',
    'FIREBASE_PROJECT_ID',
    'FIREBASE_STORAGE_BUCKET',
    'FIREBASE_MESSAGING_SENDER_ID',
    'FIREBASE_APP_ID',
    'FIREBASE_MEASUREMENT_ID',
]

@app.route('/api/firebase-config')
def firebase_config():
    cfg = {k: os.getenv(k, '') for k in _FIREBASE_ENV_KEYS}
    if not cfg.get('FIREBASE_API_KEY'):
        return jsonify({'error': 'Firebase not configured'}), 503
    return jsonify(cfg)


# ── Leak Finder ──────────────────────────────────────────────────────────────
# /leaks — the user-facing report (Phase 1: preflop stats, per position).
# /leaks/validate — dev page diffing leak_engine output against the PT4 report
# CSV ground truth in data/validation/ (aggregate fixture counts only, so it
# is intentionally unauthenticated).

_BBZ_RANGES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'data', 'bbz_leak_ranges.json')


# The seed file ships the BBZ baseline + corrections in git. Admin edits live
# in a single Firestore doc (config/leak_targets) as a sparse overlay, so a
# runtime — ephemeral filesystem, two gunicorn workers — never writes the file
# and every worker sees the same edits. Seed + overlay are merged on read here;
# both the /leaks report and the target editor go through this path.
_TARGET_OVERLAY_COLL = 'config'
_TARGET_OVERLAY_DOC = 'leak_targets'


def _seed_grid():
    """Ordered [(position, [cell, ...]), ...] straight from the seed file, or
    [] if it can't be read. Cells are the raw dicts (label, target, rec, ...)."""
    import json as _jj
    try:
        with open(_BBZ_RANGES_PATH, encoding='utf-8') as fh:
            data = _jj.load(fh)
    except Exception as exc:
        print(f"[_seed_grid] could not read {_BBZ_RANGES_PATH}: "
              f"{type(exc).__name__}: {exc}")
        return []
    return [(pos, pdata.get('stats') or [])
            for pos, pdata in (data.get('positions') or {}).items()]


def _load_target_overlay(db=None):
    """{(position, label): overlay_cell} from Firestore config/leak_targets,
    plus the doc's ('updated_at', 'updated_by'). Read fresh each call (one
    small doc) so edits appear immediately across workers; any failure yields
    an empty overlay and the report falls back to the seed."""
    meta = {'updated_at': None, 'updated_by': None}
    try:
        db = db or _get_admin_db()
        snap = db.collection(_TARGET_OVERLAY_COLL).document(_TARGET_OVERLAY_DOC).get()
        if not snap.exists:
            return {}, meta
        doc = snap.to_dict() or {}
        meta['updated_at'] = doc.get('updated_at')
        meta['updated_by'] = doc.get('updated_by')
        out = {}
        for c in (doc.get('cells') or []):
            pos, label = c.get('position'), c.get('label')
            if pos and label:
                out[(pos, label)] = c
        return out, meta
    except Exception as exc:
        # Falls back to the seed grid, so the report still renders — but log it,
        # otherwise admin target edits silently stop taking effect.
        print(f"[_load_target_overlay] overlay read failed: {type(exc).__name__}: {exc}")
        return {}, meta


def _merged_target_cells(db=None):
    """(position, label) -> {'target', 'rec', 'bands', 'overridden',
    'default_target', 'default_rec', 'default_bands'} — the seed grid with the
    Firestore overlay applied. Preserves seed order via the returned dict's
    insertion order."""
    overlay, _meta = _load_target_overlay(db)
    out = {}
    for pos, cells in _seed_grid():
        for s in cells:
            key = (pos, s['label'])
            cell = {
                'target': s.get('target'), 'rec': s.get('rec'),
                'bands': s.get('bands'),
                'default_target': s.get('target'), 'default_rec': s.get('rec'),
                'default_bands': s.get('bands'), 'overridden': False,
            }
            ov = overlay.get(key)
            if ov:
                if 'target' in ov:
                    cell['target'] = ov['target']
                if 'rec' in ov:
                    cell['rec'] = ov['rec']
                if 'bands' in ov:
                    cell['bands'] = ov['bands']
                cell['overridden'] = True
            out[key] = cell
    return out


def _load_bbz_targets(db=None):
    """{(position, stat_label): {'target': [lo, hi], 'rec': str, 'bands': {}}}
    — the seed BBZ ranges with any admin overlay merged in (design doc §7)."""
    return {k: {'target': v['target'], 'rec': v['rec'], 'bands': v.get('bands')}
            for k, v in _merged_target_cells(db).items()}


@app.route('/leaks')
def leaks_page():
    return render_template('leaks.html')


# ── Target editor (admin) ────────────────────────────────────────────────────
# View/edit the leak targets visually. Reads go through the same seed+overlay
# merge the report uses; writes land in the Firestore overlay only, keyed by
# (position, label), so the seed file stays the reconstructable baseline and
# "reset" is just deleting the overlay cell.

def _valid_target(v):
    """True if v is a valid target value: null (N/A) or [lo, hi] with
    0 <= lo < hi <= 100."""
    if v is None:
        return True
    if not (isinstance(v, (list, tuple)) and len(v) == 2):
        return False
    lo, hi = v
    return (isinstance(lo, (int, float)) and isinstance(hi, (int, float))
            and 0 <= lo < hi <= 100)


def _clean_bands(bands):
    """Validate a bands dict {band_key: target}; returns (cleaned, error).
    Unknown keys or malformed targets are rejected."""
    from leak_engine import STACK_BAND_KEYS
    if bands is None:
        return None, None
    if not isinstance(bands, dict):
        return None, 'bands must be an object'
    out = {}
    for k, v in bands.items():
        if k not in STACK_BAND_KEYS:
            return None, f'unknown band "{k}"'
        if not _valid_target(v):
            return None, f'invalid target for band "{k}"'
        out[k] = list(v) if v else None
    return out, None


@app.route('/leaks/targets')
def leaks_targets_page():
    """The target editor is a panel on /admin now; keep the old URL working."""
    return redirect('/admin#leaktargets')


@app.route('/api/leak-targets', methods=['GET'])
def leak_targets_get():
    """The full merged target grid for the editor: every (position, label)
    cell with its current + default target/rec/bands and an `overridden` flag.
    Admin-gated — this is a management view, not user-facing."""
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    try:
        from leak_engine import (POSITION_BUCKETS, ALL_STATS, STAT_STREET,
                                 STACK_BANDS)
        db = _get_admin_db()
        merged = _merged_target_cells(db)
        _overlay, meta = _load_target_overlay(db)
        # label -> street, resolved once (a label maps to one stat key).
        street_by_label = {label: STAT_STREET.get(key, '')
                           for key, label, _g in ALL_STATS}
        label_order = [label for _k, label, _g in ALL_STATS]
        positions = []
        for pos in POSITION_BUCKETS:
            rows = []
            for label in label_order:
                cell = merged.get((pos, label))
                if not cell:
                    continue
                rows.append({
                    'label': label, 'street': street_by_label.get(label, ''),
                    'target': cell['target'], 'rec': cell['rec'],
                    'bands': cell['bands'], 'overridden': cell['overridden'],
                    'default_target': cell['default_target'],
                    'default_rec': cell['default_rec'],
                    'default_bands': cell['default_bands'],
                })
            positions.append({'position': pos, 'stats': rows})
        override_count = sum(1 for c in merged.values() if c['overridden'])
        return jsonify({
            'positions': positions,
            'bands': [{'key': k, 'label': l, 'lo': lo, 'hi': hi}
                      for k, l, lo, hi in STACK_BANDS],
            'override_count': override_count,
            'updated_at': meta.get('updated_at'),
            'updated_by': meta.get('updated_by'),
        })
    except Exception as e:
        # Surface the real cause as JSON rather than an opaque 500, so the
        # editor can show it instead of failing blank.
        app.logger.exception('leak_targets_get failed')
        return jsonify({'error': 'leak-targets load failed: %s' % e}), 500


@app.route('/api/admin/leak-targets', methods=['POST'])
def leak_targets_upsert():
    """Upsert one target cell into the Firestore overlay. Body:
    {position, label, target: [lo,hi]|null, rec?, bands?: {band_key: target}}.
    The prior merged value is stored on the cell for audit."""
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    body = request.get_json(silent=True) or {}
    pos = (body.get('position') or '').strip()
    label = (body.get('label') or '').strip()
    if not pos or not label:
        return jsonify({'error': 'position and label are required'}), 400

    db = _get_admin_db()
    merged = _merged_target_cells(db)
    if (pos, label) not in merged:
        return jsonify({'error': f'unknown cell {pos}/{label}'}), 404

    cell = {'position': pos, 'label': label}
    if 'target' in body:
        if not _valid_target(body['target']):
            return jsonify({'error': 'target must be null or [lo, hi] with 0 <= lo < hi <= 100'}), 400
        cell['target'] = list(body['target']) if body['target'] else None
    if 'rec' in body:
        cell['rec'] = str(body['rec']).strip() if body['rec'] is not None else None
    if 'bands' in body:
        bands, err = _clean_bands(body['bands'])
        if err:
            return jsonify({'error': err}), 400
        cell['bands'] = bands
    if len(cell) == 2:
        return jsonify({'error': 'nothing to update'}), 400

    prev = merged[(pos, label)]
    cell['prev'] = {'target': prev['target'], 'rec': prev['rec'], 'bands': prev['bands']}
    cell['by'] = uid
    cell['at'] = int(time.time())

    # Read-modify-write of the whole cells array, so it must be transactional:
    # two admins editing different cells at once would otherwise both read the
    # same array and the second .set() would silently drop the first's edit.
    from google.cloud import firestore as gcf
    ref = db.collection(_TARGET_OVERLAY_COLL).document(_TARGET_OVERLAY_DOC)

    @gcf.transactional
    def _upsert_cell(transaction):
        snap = ref.get(transaction=transaction)
        doc = (snap.to_dict() or {}) if snap.exists else {}
        cells = [c for c in (doc.get('cells') or [])
                 if not (c.get('position') == pos and c.get('label') == label)]
        cells.append(cell)
        transaction.set(ref, {'cells': cells, 'updated_at': int(time.time()),
                              'updated_by': uid})
        return len(cells)

    overrides = _upsert_cell(db.transaction())
    return jsonify({'ok': True, 'position': pos, 'label': label,
                    'overrides': overrides})


@app.route('/api/admin/leak-targets', methods=['DELETE'])
def leak_targets_reset():
    """Remove a cell from the overlay (revert to seed default). Query:
    ?position=&label=  — or ?all=1 to clear every override."""
    uid = _verify_bearer(request)
    if not _is_admin(uid):
        return jsonify({'error': 'Forbidden'}), 403
    db = _get_admin_db()
    ref = db.collection(_TARGET_OVERLAY_COLL).document(_TARGET_OVERLAY_DOC)
    if not ref.get().exists:
        return jsonify({'ok': True, 'overrides': 0})
    clear_all = bool(request.args.get('all'))
    pos = (request.args.get('position') or '').strip()
    label = (request.args.get('label') or '').strip()
    if not clear_all and (not pos or not label):
        return jsonify({'error': 'position and label (or all=1) required'}), 400

    # Transactional for the same reason as the upsert above — the single-cell
    # delete is a read-modify-write of the shared cells array.
    from google.cloud import firestore as gcf

    @gcf.transactional
    def _apply_delete(transaction):
        snap = ref.get(transaction=transaction)
        prev = ((snap.to_dict() or {}).get('cells') or []) if snap.exists else []
        cells = [] if clear_all else [
            c for c in prev
            if not (c.get('position') == pos and c.get('label') == label)]
        transaction.set(ref, {'cells': cells, 'updated_at': int(time.time()),
                              'updated_by': uid})
        return len(cells)

    overrides = _apply_delete(db.transaction())
    if clear_all:
        return jsonify({'ok': True, 'overrides': 0})
    return jsonify({'ok': True, 'position': pos, 'label': label,
                    'overrides': overrides})


# ── Per-tournament leak cache ────────────────────────────────────────────────
# A tournament's hands compress to a count-vector (~490 numbers) that is
# additive: summing vectors equals re-counting hands. Caching those makes a
# filtered report one Firestore read plus arithmetic, instead of re-reading
# and re-parsing every hand blob (~10s) on every filter change.

def _leak_cache_ref(db, uid, tid):
    return db.collection('users').document(uid).collection('leak_cache').document(tid)


def _build_leak_vector(uid, tid):
    """Parse one tournament's hands and compress them to a count-vector.
    Returns (vector, hands_used, hands_skipped) or None when unreadable."""
    from hand_exporter import records_to_ps_text
    from leak_engine import parse_ps_text, validate_pot, hands_to_vector

    records, _doc = _fetch_tournament_records(uid, tid)
    if not records:
        return None
    # No blind-ladder resolution here: it only affects the cosmetic "Level"
    # header, which the leak engine ignores, and costs a Firestore scan.
    text, _stats = records_to_ps_text(records)
    hands, skipped = [], 0
    for h in parse_ps_text(text):
        if validate_pot(h):
            skipped += 1          # PT4 drops these too — see design doc §5
        else:
            hands.append(h)
    return hands_to_vector(hands, scheme='report'), len(hands), skipped


def _load_leak_vectors(db, uid, tourney_docs, budget_s=25):
    """
    {tid: {'vector', 'hands', 'skipped'}} for the given tournaments, served
    from cache and rebuilt where missing or stale. A stale entry is one whose
    engine version or source `updated_at` no longer matches, so re-imports and
    stat-definition changes both invalidate automatically.

    Rebuilds are bounded by `budget_s`: anything past the budget is left for a
    later request and reported as pending, so a cold cache degrades to a
    partial report instead of hanging.
    """
    import json as _jj
    import time as _tt
    from leak_engine import ENGINE_VERSION

    cached = {}
    for snap in db.collection('users').document(uid).collection('leak_cache').get():
        cached[snap.id] = snap.to_dict()

    out, pending = {}, 0
    deadline = _tt.monotonic() + budget_s
    for tid, meta in tourney_docs.items():
        entry = cached.get(tid)
        fresh = (entry
                 and entry.get('v') == ENGINE_VERSION
                 and entry.get('src_updated_at') == meta.get('updated_at'))
        if fresh:
            try:
                out[tid] = {'vector': _jj.loads(entry['data']),
                            'hands': entry.get('hands', 0),
                            'skipped': entry.get('skipped', 0)}
                continue
            except (ValueError, KeyError):
                pass              # corrupt entry — fall through and rebuild
        if _tt.monotonic() >= deadline:
            pending += 1
            continue
        built = _build_leak_vector(uid, tid)
        if not built:
            continue
        vector, n_hands, n_skipped = built
        out[tid] = {'vector': vector, 'hands': n_hands, 'skipped': n_skipped}
        try:
            _leak_cache_ref(db, uid, tid).set({
                'v': ENGINE_VERSION,
                'src_updated_at': meta.get('updated_at'),
                'data': _jj.dumps(vector, separators=(',', ':')),
                'hands': n_hands, 'skipped': n_skipped,
                'built_at': int(_tt.time()),
            })
        except Exception as exc:
            # Best-effort, never fatal — but log it, because a persistently
            # failing cache write turns every leak report into a full rebuild.
            print(f"[_load_leak_vectors] cache write failed for tid={tid}: "
                  f"{type(exc).__name__}: {exc}")
    return out, pending


@app.route('/api/leaks')
def leaks_api():
    from leak_engine import (POSITION_BUCKETS, ALL_STATS, STAT_STREET, classify,
                             delta_from_target, MIN_SAMPLE, CONFIDENCE_LEVELS,
                             merge_vectors, STACK_BANDS, STACK_BAND_KEYS)

    uid = _verify_bearer(request)   # inits the Admin SDK internally
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401
    # Admin-only while the Leak Finder is still being built out — it lives under
    # /admin now rather than being offered to Pro users. The message surfaces
    # directly in the page's error banner, so it reads for a human.
    if not _is_admin(uid):
        return jsonify({'error': 'This page is admin-only — your account is '
                                 'not in the admin list.'}), 403
    db = _get_admin_db()

    # ── Available tournaments (filter options) ──
    # Only real-money MTTs make a meaningful leak report, so selectability keys
    # off hand_parser.classify_game (room.mtt AND a real club room name) rather
    # than room.mtt alone. The scraper is only supposed to import tournaments
    # (yellow cash-game tiles are skipped), but cash sessions have made it in via
    # manual/legacy imports, and play-money games — MTTs included — get imported
    # whenever they show up in the hand history. Both are marked is_mtt=False on
    # the wire so neither can enter a leak report.
    cutoff = _history_cutoff_ts(uid)
    tourneys = {}
    for doc in db.collection('users').document(uid).collection('tournaments').get():
        d = doc.to_dict()
        if _is_expired(d, cutoff):
            continue      # outside the caller's history window — not selectable
        room = d.get('room_name') or ''
        tourneys[doc.id] = {
            'room_key': _norm_room_name(room) or '(unnamed)',
            'room_label': room or '(unnamed)',
            'is_mtt': _classify_doc(d)['category'] == CATEGORY_TOURNAMENT,
            'earliest_ts': d.get('earliest_ts'),
            'updated_at': d.get('updated_at'),
            'hands': d.get('hands', 0),
        }

    # Grouped by (name, is_mtt) rather than name alone, so a cash table that
    # happens to share a normalized name with a tournament still gets its own
    # disabled entry instead of merging into the selectable list.
    rooms = {}
    for meta in tourneys.values():
        key = (meta['room_key'], meta['is_mtt'])
        r = rooms.setdefault(key,
                             {'key': meta['room_key'], 'label': meta['room_label'],
                              'is_mtt': meta['is_mtt'], 'tournaments': 0, 'hands': 0})
        r['tournaments'] += 1
        r['hands'] += meta['hands']
    all_ts = [m['earliest_ts'] for m in tourneys.values() if m['earliest_ts']]

    # ── Apply filters (they select whole tournaments, never partial ones) ──
    want_rooms = {r for r in (request.args.get('rooms') or '').split(',') if r}
    def _ts_arg(name):
        raw = (request.args.get(name) or '').strip()
        if not raw:
            return None
        try:
            from datetime import datetime as _d, timezone as _tz
            return int(_d.strptime(raw, '%Y-%m-%d')
                       .replace(tzinfo=_tz.utc).timestamp())
        except ValueError:
            return None
    ts_from, ts_to = _ts_arg('from'), _ts_arg('to')
    if ts_to is not None:
        ts_to += 86399            # 'to' is inclusive of the whole day

    selected = {}
    for tid, meta in tourneys.items():
        if not meta['is_mtt']:
            continue          # cash / play-money games are never selectable,
                              # whatever `rooms` asks for
        if want_rooms and meta['room_key'] not in want_rooms:
            continue
        ts = meta['earliest_ts']
        if ts_from is not None and (ts is None or ts < ts_from):
            continue
        if ts_to is not None and (ts is None or ts > ts_to):
            continue
        selected[tid] = meta

    # ── Cached vectors → one summed aggregate ──
    unadjusted = 0
    try:
        from equity import set_budget, skipped_count, save_cache
        set_budget(20)
    except Exception as exc:
        print(f"[leaks_api] equity module unavailable, continuing without "
              f"all-in adjustment: {type(exc).__name__}: {exc}")
        set_budget = skipped_count = save_cache = None

    loaded, pending = _load_leak_vectors(db, uid, selected)

    if save_cache:
        try:
            save_cache()          # persist any newly enumerated all-in equities
            unadjusted = skipped_count()
        except Exception as exc:
            print(f"[leaks_api] equity cache save failed: {type(exc).__name__}: {exc}")
        set_budget(None)

    agg = merge_vectors(v['vector'] for v in loaded.values())
    total_skipped = sum(v['skipped'] for v in loaded.values())
    targets = _load_bbz_targets(db)

    # Depth band: 'all' (position total) or one STACK_BANDS key. An unknown
    # value falls back to 'all' rather than erroring.
    band = (request.args.get('band') or 'all').strip()
    if band not in STACK_BAND_KEYS:
        band = 'all'

    # Per-band hand counts (always computed, so the client can render the
    # selector and disable depths with no data regardless of the current view).
    band_hands = {bk: sum(agg['positions'][b].get('bands', {}).get(bk, {}).get('hands', 0)
                          for b in POSITION_BUCKETS)
                  for bk in STACK_BAND_KEYS}

    def _source(p):
        """The counts bucket the current band selects: the position total, or
        its band sub-partition (an empty stand-in when that band has no data)."""
        if band == 'all':
            return p
        return (p.get('bands') or {}).get(band) or {
            'hands': 0, 'bb': 0.0, 'bb_adj': 0.0,
            'stats': {k: {'made': 0, 'opp': 0} for k, _l, _g in ALL_STATS}}

    positions = []
    for bucket in POSITION_BUCKETS:
        p = agg['positions'][bucket]
        src = _source(p)
        rows = []
        for key, label, _is_global in ALL_STATS:
            st = src['stats'][key]
            made, opp = st['made'], st['opp']
            t = targets.get((bucket, label)) or {}
            pct = round(made / opp * 100, 2) if opp else None
            # A band-specific target wins when the cell defines one for this
            # depth; otherwise the depth view inherits the blended target.
            target = t.get('target')
            cell_bands = t.get('bands') or {}
            if band != 'all' and band in cell_bands:
                target = cell_bands[band]
            # min_sample=0 leaves the verdict ungated: the client applies the
            # sample-size gate itself against the Confidence level the reader
            # picked, so changing levels never costs a round trip. A row with
            # no opportunities still lands None here, via pct.
            result = classify(pct, target, opp, min_sample=0)
            delta = delta_from_target(pct, target)
            rows.append({'key': key, 'label': label, 'pct': pct,
                         'made': made, 'opp': opp, 'street': STAT_STREET[key],
                         'target': target, 'rec': t.get('rec'), 'result': result,
                         'delta': round(delta, 3) if delta is not None else None})
        n = src['hands']
        positions.append({
            'position': bucket, 'hands': n, 'stats': rows,
            'winrate_bb100': round(src['bb_adj'] / n * 100, 2) if n else None,
            'winrate_raw_bb100': round(src['bb'] / n * 100, 2) if n else None,
        })

    total_hands = sum(p['hands'] for p in positions)
    total_adj = sum(_source(agg['positions'][b])['bb_adj'] for b in POSITION_BUCKETS)
    total_raw = sum(_source(agg['positions'][b])['bb'] for b in POSITION_BUCKETS)
    from datetime import datetime as _dt2, timezone as _tz2
    def _day(ts):
        return _dt2.fromtimestamp(ts, tz=_tz2.utc).strftime('%Y-%m-%d') if ts else None

    return jsonify({
        'meta': {
            'tournaments': len(loaded), 'hands': total_hands,
            'hands_skipped': total_skipped, 'phase': 5, 'min_sample': MIN_SAMPLE,
            'confidence_levels': CONFIDENCE_LEVELS,
            'hands_unadjusted': unadjusted,
            'tournaments_pending': pending,
            'winrate_bb100': round(total_adj / total_hands * 100, 2) if total_hands else None,
            'winrate_raw_bb100': round(total_raw / total_hands * 100, 2) if total_hands else None,
            'band': band,
            'bands': [{'key': k, 'label': l, 'hands': band_hands.get(k, 0)}
                      for k, l, _lo, _hi in STACK_BANDS],
        },
        'filters': {
            'rooms': sorted(rooms.values(), key=lambda r: -r['hands']),
            'date_min': _day(min(all_ts)) if all_ts else None,
            'date_max': _day(max(all_ts)) if all_ts else None,
            'applied': {'rooms': sorted(want_rooms),
                        'from': request.args.get('from') or None,
                        'to': request.args.get('to') or None},
        },
        'positions': positions,
    })


@app.route('/leaks/validate')
def leaks_validate_page():
    return render_template('leaks_validate.html')


@app.route('/api/leaks/validate')
def leaks_validate_api():
    from leak_validation import run_all
    try:
        return jsonify(run_all())
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# ── PWA / TWA support ─────────────────────────────────────────────────────────

@app.route('/tournaments')
def tournaments_page():
    return render_template('tournaments.html')

@app.route('/admin')
def admin_page():
    """Admin console. Unlisted for non-admins — the APIs it calls are the gate."""
    return render_template('admin.html')

@app.route('/offline')
def offline():
    """Minimal offline fallback page served by the service worker."""
    return render_template('offline.html')

@app.route('/static/sw.js')
def service_worker():
    """Serve the service worker from /static/ but with the right scope headers."""
    response = send_from_directory('static', 'sw.js',
                                   mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response

@app.route('/.well-known/assetlinks.json')
def asset_links():
    """
    Digital Asset Links — required for TWA (Trusted Web Activity) on Google Play.
    Replace the placeholder sha256_cert_fingerprints value with your actual
    signing key fingerprint from the Play Console / Bubblewrap output.
    """
    links = [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                # TODO: replace with your actual Android app package name and fingerprint
                "package_name": "com.yourname.pppokerha",
                "sha256_cert_fingerprints": [
                    "REPLACE_WITH_SHA256_FINGERPRINT_FROM_PLAY_CONSOLE"
                ]
            }
        }
    ]
    return Response(
        __import__('json').dumps(links, indent=2),
        mimetype='application/json',
        headers={'Cache-Control': 'no-cache'}
    )


if __name__ == "__main__":
    # Local dev only (prod runs under gunicorn via the Procfile). Bound to
    # localhost and debug off by default: the Werkzeug debugger is an RCE vector
    # on any reachable interface. Opt in with FLASK_DEBUG=1 when you need it.
    app.run(host="127.0.0.1", port=5000,
            debug=os.getenv("FLASK_DEBUG") == "1")
