"""
test_tiering.py — the anon/free/pro rules, end to end, against a fake Firestore
and a fake Storage bucket so the handlers run without credentials or network.

Everything here is a rule someone can lose money on: an export that should have
cost a survey and didn't, a survey postback that paid out twice, a free account
reading history it no longer has, an ad token spent more than once. The counters
live in Firestore precisely because a process-global can't be trusted across
gunicorn workers, so the tests drive the real handlers through the real
transactions rather than asserting on helper return values alone.

    python test_tiering.py
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')
# Read at import time by app.py, so they must be set before the import below.
os.environ['AD_TOKEN_SECRET']      = 'ad-secret'
os.environ['ANON_SESSION_SECRET']  = 'anon-secret'
os.environ['CPX_SECURE_HASH']      = 'cpx-secret'
os.environ['CPX_APP_ID']           = 'cpx-app'
os.environ['TALLY_SIGNING_SECRET'] = 'tally-secret'

FREE_UID = 'uid-free'
PRO_UID  = 'uid-pro'

_FAILURES = []


def check(label, cond, extra=''):
    if cond:
        print(f'  [PASS] {label}')
    else:
        _FAILURES.append(label)
        print(f'  [FAIL] {label} {extra}')


# ── Fake Firestore ───────────────────────────────────────────────────────────
# Paths are tuples: ('users', uid, 'tournaments', tid). Enough of the surface to
# run the handlers: get/set/update/create, subcollections, and transactions.

class _Snap:
    def __init__(self, doc_id, data, ref=None):
        self.id, self._data, self.exists = doc_id, data, data is not None
        self.reference = ref

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def collection(self, name):
        return _Col(self._store, self._path + (name,))

    def get(self, transaction=None):
        return _Snap(self._path[-1], self._store.get(self._path), self)

    def set(self, data, merge=False):
        cur = dict(self._store.get(self._path) or {}) if merge else {}
        cur.update(data)
        self._store.put(self._path, cur)

    def update(self, data):
        cur = self._store.get(self._path)
        if cur is None:
            raise RuntimeError(f'update() on missing doc {self._path}')
        cur = dict(cur)
        cur.update(data)
        self._store.put(self._path, cur)

    def create(self, data):
        from google.api_core import exceptions as gexc
        if self._store.get(self._path) is not None:
            raise gexc.AlreadyExists(f'{self._path} exists')
        self._store.put(self._path, dict(data))


class _Col:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def document(self, doc_id):
        return _Doc(self._store, self._path + (doc_id,))

    def get(self):
        out = []
        for path, data in self._store.items():
            if path[:-1] == self._path and len(path) == len(self._path) + 1:
                out.append(_Snap(path[-1], data, _Doc(self._store, path)))
        return out


class _Txn:
    """Applies straight through — the fake is single-threaded, so the only thing
    worth exercising is that reads and writes go to the right paths."""

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)

    def update(self, ref, data):
        ref.update(data)

    def create(self, ref, data):
        ref.create(data)


class FakeDB:
    def __init__(self):
        self._d = {}

    def collection(self, name):
        return _Col(self, (name,))

    def transaction(self):
        return _Txn()

    def get(self, path):
        return self._d.get(path)

    def put(self, path, data):
        self._d[path] = data

    def items(self):
        return list(self._d.items())


# ── Fake Storage ─────────────────────────────────────────────────────────────

class _Blob:
    def __init__(self, bucket, name):
        self._bucket, self.name = bucket, name
        self.metadata = None

    def upload_from_string(self, data, content_type=None):
        self._bucket.objects[self.name] = (data, dict(self.metadata or {}))

    def exists(self):
        return self.name in self._bucket.objects

    def download_as_bytes(self):
        data = self._bucket.objects[self.name][0]
        return data.encode() if isinstance(data, str) else data

    def delete(self):
        self._bucket.objects.pop(self.name, None)


class FakeBucket:
    def __init__(self):
        self.objects = {}

    def blob(self, name):
        b = _Blob(self, name)
        if name in self.objects:
            b.metadata = dict(self.objects[name][1])
        return b

    def list_blobs(self, prefix='', max_results=None):
        out = []
        for name in list(self.objects):
            if name.startswith(prefix):
                out.append(self.blob(name))
        return out[:max_results] if max_results else out


# ── Harness ──────────────────────────────────────────────────────────────────

import google.cloud.firestore as _gcf
_gcf.transactional = lambda fn: fn        # the fake transaction needs no retry loop

import app as A                                                    # noqa: E402

DB     = FakeDB()
BUCKET = FakeBucket()
_signed_in = {'uid': None}

A._get_admin_db      = lambda: DB
A._get_admin_bucket  = lambda: BUCKET
A._verify_bearer     = lambda req: _signed_in['uid']
A._verify_bearer_claims = lambda req: ({'uid': _signed_in['uid'], 'email': 'x@y.z'}
                                       if _signed_in['uid'] else None)
A.export_pokerstars  = lambda records, platform='', blind_levels_by_room=None: (
    _write_tmp(records), [])
A._blind_levels_by_room = lambda records: {}
A._score_import      = lambda claims, new_hands: None


def _write_tmp(records):
    import tempfile
    fd, path = tempfile.mkstemp(suffix='.txt')
    with os.fdopen(fd, 'w') as fh:
        fh.write(f'{len(records)} hands')
    return path


def as_user(uid):
    _signed_in['uid'] = uid


CLIENT = A.app.test_client()


def _reset_user(uid, is_pro=False):
    DB.put(('users', uid), {'is_pro': is_pro})
    for path in [p for p, _ in DB.items()
                 if len(p) > 2 and p[0] == 'users' and p[1] == uid
                 and p[2] in ('ad_jtis', 'survey_completions', 'quota', 'gate_events')]:
        DB._d.pop(path, None)


def _put_tournament(uid, tid, earliest_ts, hands=3):
    DB.put(('users', uid, 'tournaments', tid),
           {'tourney_id': tid, 'room_name': 'DEEP FREEZE', 'is_mtt': True,
            'earliest_ts': earliest_ts, 'hands': hands,
            'storage_path': f'tournaments/{uid}/{tid}.json'})


def _records(tid, n=3):
    # gameid is prefix-tourneyid-seq (see hand_parser.extract_tourney_id), so the
    # tournament id must be the middle segment for the routes to match on it.
    return [{'summary': {'D': f'1858-{tid}-{i}', 'C': 1700000000 + i},
             'full_hand': {'info': {'room': {'room_name': 'DEEP FREEZE'}}}}
            for i in range(n)]


def _stub_tournament_records(mapping):
    A._fetch_tournament_records = lambda uid, tid: (
        (mapping.get(tid), DB.get(('users', uid, 'tournaments', tid)))
        if tid in mapping else (None, None))


NOW = int(time.time())
OLD_TS    = NOW - 30 * 86400
RECENT_TS = NOW - 2 * 86400


# ── 1. Quota bookkeeping ─────────────────────────────────────────────────────

def test_quota():
    print('quota')
    _reset_user(FREE_UID)
    with A.app.test_request_context('/'):
        check('lazily zero before any write',
              A._quota_state(FREE_UID)['imports'] == 0)

    # A stale day must read as zero without anyone rewriting it first.
    DB.put(('users', FREE_UID), {'is_pro': False,
                                 'quota': {'day': '2000-01-01', 'imports': 3,
                                           'hand_exports': 5, 'tourney_exports': 1}})
    with A.app.test_request_context('/'):
        state = A._quota_state(FREE_UID)
    check('previous UTC day rolls over on read',
          state['imports'] == 0 and state['hand_exports'] == 0, str(state))

    with A.app.test_request_context('/'):
        A._bump_quota(FREE_UID, 'imports')
        A._bump_quota(FREE_UID, 'imports')
        state = A._quota_state(FREE_UID)
    check('bump increments today only',
          state['imports'] == 2 and state['day'] == A._utc_day(), str(state))
    check('rollover rewrote the stored day',
          DB.get(('users', FREE_UID))['quota']['day'] == A._utc_day())

    # The user doc is created lazily — a user who has never been written must
    # not 500 the first time they export.
    DB._d.pop(('users', 'uid-fresh'), None)
    with A.app.test_request_context('/'):
        A._bump_quota('uid-fresh', 'hand_exports')
    check('quota creates the user doc when absent',
          DB.get(('users', 'uid-fresh'))['quota']['hand_exports'] == 1)


# ── 2. Survey credits ────────────────────────────────────────────────────────

def test_credits():
    print('credits')
    _reset_user(FREE_UID)
    with A.app.test_request_context('/'):
        for _ in range(5):
            A._grant_credit(FREE_UID, 'hand')
        for _ in range(3):
            A._grant_credit(FREE_UID, 'tourney')
        credits = A._credits(FREE_UID)
    check('hand credits cap at 3', credits['hand'] == 3, str(credits))
    check('tourney credits cap at 1', credits['tourney'] == 1, str(credits))

    with A.app.test_request_context('/'):
        spent = A._consume_credit(FREE_UID, 'tourney')
        again = A._consume_credit(FREE_UID, 'tourney')
        left  = A._credits(FREE_UID)
    check('consume spends one credit', spent is True and left['tourney'] == 0)
    check('consume at zero is a no-op', again is False)


# ── 2b. Tourney-export: lifetime-free + weekly counter ──────────────────────

def test_tourney_export_state():
    print('tourney export state')
    _reset_user(FREE_UID)
    DB._d.pop(('users', FREE_UID, 'quota', 'tourney_export'), None)

    with A.app.test_request_context('/'):
        this_week = A._current_iso_week()
        state = A._tourney_export_state(FREE_UID)
    check('brand-new user has not used the lifetime freebie',
          state['lifetime_free_used'] is False, str(state))
    check('brand-new user has zero weekly usage', state['current_week_used'] == 0, str(state))
    check('current_week_iso is resolved server-side to this ISO week',
          state['current_week_iso'] == this_week, str(state))
    check('reading state performs no write',
          DB.get(('users', FREE_UID, 'quota', 'tourney_export')) is None)

    with A.app.test_request_context('/'):
        new_state = A._bump_tourney_export_usage(FREE_UID)
    check('the first bump spends the lifetime freebie',
          new_state['lifetime_free_used'] is True, str(new_state))
    check('spending the freebie does not count against the weekly cap',
          new_state['current_week_used'] == 0, str(new_state))

    with A.app.test_request_context('/'):
        state = A._tourney_export_state(FREE_UID)
    check('lifetime-already-used is reflected on the next read',
          state['lifetime_free_used'] is True, str(state))
    check('lifetime_free_used_at was stamped', state['lifetime_free_used_at'] is not None)

    with A.app.test_request_context('/'):
        second = A._bump_tourney_export_usage(FREE_UID)
    check('the second bump (freebie spent) increments this week\'s counter',
          second['current_week_used'] == 1, str(second))
    check('lifetime_free_used_at is preserved, not re-stamped',
          second['lifetime_free_used_at'] == new_state['lifetime_free_used_at'])

    # Week rollover: a stored week that isn't the current one must read (and
    # then bump) as if the counter were freshly zero, without any nightly job.
    stale = {'lifetime_free_used': True, 'lifetime_free_used_at': 't0',
             'current_week_iso': '2000-W01', 'current_week_used': 5,
             'last_reset_at': 't0'}
    DB.put(('users', FREE_UID, 'quota', 'tourney_export'), stale)
    with A.app.test_request_context('/'):
        this_week = A._current_iso_week()
        rolled = A._tourney_export_state(FREE_UID)
    check('a stale ISO week reads current_week_used as 0',
          rolled['current_week_used'] == 0, str(rolled))
    check('the lifetime flag survives a week rollover',
          rolled['lifetime_free_used'] is True, str(rolled))
    check('current_week_iso reported is the current week, not the stale one',
          rolled['current_week_iso'] == this_week, str(rolled))

    with A.app.test_request_context('/'):
        bumped = A._bump_tourney_export_usage(FREE_UID)
    check('a bump after rollover starts this week at 1, not 6',
          bumped['current_week_used'] == 1, str(bumped))
    check('and rewrites the stored week to the current one',
          bumped['current_week_iso'] == this_week, str(bumped))

    # A user who has never been written to Firestore at all must not 500.
    DB._d.pop(('users', 'uid-fresh-tourney'), None)
    with A.app.test_request_context('/'):
        fresh_state = A._tourney_export_state('uid-fresh-tourney')
        fresh_bump = A._bump_tourney_export_usage('uid-fresh-tourney')
    check('a never-seen uid reads a clean default state',
          fresh_state['lifetime_free_used'] is False, str(fresh_state))
    check('and can still spend its lifetime freebie',
          fresh_bump is not None and fresh_bump['lifetime_free_used'] is True,
          str(fresh_bump))


# ── 2c. Gate-event history ───────────────────────────────────────────────────

def test_gate_events():
    print('gate events')
    _reset_user(FREE_UID)
    for path in [p for p, _ in DB.items()
                 if len(p) > 2 and p[0] == 'users' and p[1] == FREE_UID and p[2] == 'gate_events']:
        DB._d.pop(path, None)

    with A.app.test_request_context('/'):
        A._record_gate_event(FREE_UID, 'tourney_export', True, provider='stub',
                             completion_id='cmp-1')
        A._record_gate_event(FREE_UID, 'hand_export', False)
        A._record_gate_event(FREE_UID, 'import', True, provider='cpx', completion_id='trans-9')

    events = [data for path, data in DB.items()
              if len(path) == 4 and path[0] == 'users' and path[1] == FREE_UID
              and path[2] == 'gate_events']
    check('three events were recorded', len(events) == 3, str(events))

    by_kind = {e['kind']: e for e in events}
    tourney_evt = by_kind['tourney_export']
    check('a gated tourney_export event records its provider and completion id',
          tourney_evt['gated'] is True and tourney_evt['gate_provider'] == 'stub'
          and tourney_evt['gate_completion_id'] == 'cmp-1',
          str(tourney_evt))
    check('the event carries a written timestamp',
          tourney_evt.get('at') == _gcf.SERVER_TIMESTAMP, str(tourney_evt.get('at')))
    check('an ungated event records gated=False with no provider',
          by_kind['hand_export']['gated'] is False
          and by_kind['hand_export']['gate_provider'] is None,
          str(by_kind.get('hand_export')))
    check('gate_provider is a free-form string, not a fixed set — cpx accepted',
          by_kind['import']['gate_provider'] == 'cpx', str(by_kind.get('import')))


# ── 3. Ad tokens ─────────────────────────────────────────────────────────────

def test_ad_tokens():
    print('ad tokens')
    _reset_user(FREE_UID)
    with A.app.test_request_context('/'):
        token, exp = A._issue_ad_token(FREE_UID, 'hand')
        check('token carries a future expiry', exp > time.time())
        check('valid token verifies', A._verify_ad_token(token, FREE_UID, 'hand'))
        check('single use — a replay is refused',
              A._verify_ad_token(token, FREE_UID, 'hand') is False)

        other, _ = A._issue_ad_token(FREE_UID, 'hand')
        check('kind is scoped to the endpoint',
              A._verify_ad_token(other, FREE_UID, 'tourney') is False)
        check('another user cannot spend it',
              A._verify_ad_token(other, PRO_UID, 'hand') is False)

        encoded = other.split('.')[0]
        check('a forged signature is refused',
              A._verify_ad_token(f'{encoded}.deadbeef', FREE_UID, 'hand') is False)

        stale = base64.urlsafe_b64encode(
            f'{FREE_UID}|hand|{int(time.time()) - 1}|jti'.encode()).decode().rstrip('=')
        check('an expired token is refused',
              A._verify_ad_token(f'{stale}.{A._sign(A._AD_TOKEN_SECRET, stale)}',
                                 FREE_UID, 'hand') is False)


# ── 4. Export gates ──────────────────────────────────────────────────────────

def _export_hand(tid='trecent', hand='1858-trecent-0', headers=None):
    return CLIENT.post(f'/api/tournaments/{tid}/export/hand',
                       json={'hand_id': hand, 'platform': 'PokerTracker'},
                       headers=headers or {})


def _export_tourney(tid='trecent'):
    return CLIENT.post(f'/api/tournaments/{tid}/export', json={'platform': 'PokerTracker'})


def test_export_gates():
    print('export gates')
    _reset_user(FREE_UID)
    _reset_user(PRO_UID, is_pro=True)
    _put_tournament(FREE_UID, 'trecent', RECENT_TS)
    _put_tournament(PRO_UID, 'trecent', RECENT_TS)
    _stub_tournament_records({'trecent': _records('trecent')})

    as_user(None)
    res = _export_hand()
    check('anonymous export is refused with login_required',
          res.status_code == 401 and res.get_json().get('error') == 'login_required',
          str(res.status_code))
    res = CLIENT.post('/api/export/pokerstars', json={'tourney_ids': ['trecent']})
    check('anonymous session export is refused too', res.status_code == 401)

    as_user(FREE_UID)
    codes = [_export_hand().status_code for _ in range(2)]
    check('free hand exports 1 and 2 need no survey', codes == [200, 200], str(codes))

    res = _export_hand()
    body = res.get_json()
    check('hand export 3 asks for a survey',
          res.status_code == 402 and body.get('error') == 'survey_required'
          and body.get('kind') == 'hand', f'{res.status_code} {body}')

    with A.app.test_request_context('/'):
        A._grant_credit(FREE_UID, 'hand')
    res = _export_hand()
    check('a survey credit unlocks hand export 3', res.status_code == 200,
          str(res.status_code))
    with A.app.test_request_context('/'):
        after = A._credits(FREE_UID)
    check('the credit was spent', after['hand'] == 0, str(after))

    # 4 and 5 via ad tokens, which is the other half of the same contract.
    for n in (4, 5):
        with A.app.test_request_context('/'):
            token, _ = A._issue_ad_token(FREE_UID, 'hand')
        res = _export_hand(headers={'X-Ad-Token': token})
        check(f'ad token unlocks hand export {n}', res.status_code == 200,
              str(res.status_code))

    with A.app.test_request_context('/'):
        A._grant_credit(FREE_UID, 'hand')
    res = _export_hand()
    body = res.get_json()
    check('hand export 6 is over the daily cap, credit or not',
          res.status_code == 402 and body.get('error') == 'quota_exceeded'
          and body.get('upgrade') is True, f'{res.status_code} {body}')

    with A.app.test_request_context('/'):
        hand_events = [d for _p, d in DB.items()
                      if len(_p) == 4 and _p[0] == 'users' and _p[1] == FREE_UID
                      and _p[2] == 'gate_events' and d.get('kind') == 'hand_export']
    check('every successful hand export left a gate_events row (AC4)',
          len(hand_events) == 5, str(len(hand_events)))
    check('the free ones are recorded ungated',
          sum(1 for e in hand_events if e['gated'] is False) == 2, str(hand_events))
    check('the credit/token-unlocked ones are recorded gated',
          sum(1 for e in hand_events if e['gated'] is True) == 3, str(hand_events))

    # Per-tournament exports: 1 free for the lifetime of the account, then
    # survey-gated (CPX, unchanged) once per ISO week — see AC3.
    res = _export_tourney()
    check('the lifetime-free tourney export needs no survey',
          res.status_code == 200, str(res.status_code))
    with A.app.test_request_context('/'):
        state = A._tourney_export_state(FREE_UID)
    check('the lifetime freebie is now marked spent',
          state['lifetime_free_used'] is True, str(state))

    res = _export_tourney()
    check('the next tourney export (lifetime spent) asks for a survey',
          res.status_code == 402 and res.get_json().get('error') == 'survey_required',
          str(res.status_code))
    with A.app.test_request_context('/'):
        A._grant_credit(FREE_UID, 'tourney')
    check('tournament export succeeds on a credit', _export_tourney().status_code == 200)
    with A.app.test_request_context('/'):
        weekly_state = A._tourney_export_state(FREE_UID)
    check('the weekly counter advanced by exactly one',
          weekly_state['current_week_used'] == 1, str(weekly_state))

    with A.app.test_request_context('/'):
        A._grant_credit(FREE_UID, 'tourney')
    res = _export_tourney()
    check('a second gated tourney export the same week is over the weekly cap, credit or not',
          res.status_code == 402 and res.get_json().get('error') == 'quota_exceeded',
          str(res.status_code))
    with A.app.test_request_context('/'):
        # The credit granted just above was never spent — quota_exceeded blocks
        # before the credit check runs at all — so it is still banked. Spend it
        # back down to zero so the rollover check below starts from a clean
        # "no unlock banked" state instead of silently riding on this leftover.
        A._consume_credit(FREE_UID, 'tourney')
        check('no tourney credit is banked going into the rollover check',
              A._credits(FREE_UID)['tourney'] == 0)

    # Week rollover: a stored current_week_iso from a prior week must not keep
    # counting against this week's limit (mirrors test_tourney_export_state's
    # rollover, but exercised through the actual route this time).
    DB.put(('users', FREE_UID, 'quota', 'tourney_export'),
           dict(DB.get(('users', FREE_UID, 'quota', 'tourney_export')) or {},
                current_week_iso='2000-W01', current_week_used=99))
    res = _export_tourney()
    check('a stale current_week_iso reads as this week with zero used, so a '
          'survey is asked for again rather than staying capped',
          res.status_code == 402 and res.get_json().get('error') == 'survey_required',
          str(res.status_code))
    with A.app.test_request_context('/'):
        A._grant_credit(FREE_UID, 'tourney')
    check('the export succeeds once unlocked in the new week',
          _export_tourney().status_code == 200)
    with A.app.test_request_context('/'):
        rolled_state = A._tourney_export_state(FREE_UID)
    check('the weekly counter is 1 for the new week, not 100',
          rolled_state['current_week_used'] == 1, str(rolled_state))

    with A.app.test_request_context('/'):
        tourney_events = [d for _p, d in DB.items()
                          if len(_p) == 4 and _p[0] == 'users' and _p[1] == FREE_UID
                          and _p[2] == 'gate_events' and d.get('kind') == 'tourney_export']
    check('the lifetime-free grant and both credit-unlocked grants left a row',
          len(tourney_events) == 3, str(tourney_events))
    check('the lifetime grant is ungated',
          any(e['gated'] is False for e in tourney_events), str(tourney_events))
    check('the credit-unlocked grant is gated',
          any(e['gated'] is True for e in tourney_events), str(tourney_events))

    res = CLIENT.post('/api/export/pokerstars', json={'tourney_ids': ['trecent']})
    body = res.get_json()
    check('free whole-session export is upgrade-only, with no survey path',
          res.status_code == 403 and body.get('error') == 'upgrade_required'
          and body.get('feature') == 'full_session_export', f'{res.status_code} {body}')
    res = CLIENT.post('/api/export/json/all', json={'tourney_ids': ['trecent']})
    check('free whole-session JSON export is upgrade-only too',
          res.status_code == 403 and res.get_json().get('error') == 'upgrade_required')

    as_user(PRO_UID)
    codes = [_export_hand().status_code for _ in range(8)]
    check('pro hand exports are uncapped', codes == [200] * 8, str(codes))
    codes = [_export_tourney().status_code for _ in range(3)]
    check('pro tournament exports are uncapped', codes == [200] * 3, str(codes))
    check('pro whole-session export works',
          CLIENT.post('/api/export/pokerstars',
                      json={'tourney_ids': ['trecent']}).status_code == 200)
    with A.app.test_request_context('/'):
        check('pro exports are not counted against any quota',
              A._quota_state(PRO_UID)['hand_exports'] == 0)


# ── 4b. Export ads config (admin-controlled hard/soft survey limits) ────────

def test_export_ads_config():
    print('export ads config')
    _reset_user(FREE_UID)
    _put_tournament(FREE_UID, 'trecent', RECENT_TS)
    _stub_tournament_records({'trecent': _records('trecent')})
    DB._d.pop(('config', 'export_ads'), None)
    DB.put(('config', 'admins'), {'uids': ['uid-admin']})

    DEFAULT_CFG = {
        'hand_hard_limit':       A.FREE_HAND_EXPORTS_PER_DAY,
        'hand_soft_limit':       A.FREE_HAND_EXPORTS_PER_DAY - A.FREE_HAND_EXPORTS_UNGATED,
        'tourney_hard_limit':    A.FREE_TOURNEY_EXPORTS_DAY,
        'tourney_soft_limit':    A.FREE_TOURNEY_EXPORTS_DAY,
        'tourney_lifetime_free': 1,
        'tourney_weekly_limit':  1,
    }

    # The *public* /api/export-ads-config is a reshaped, nested view (see
    # export_ads_config_get in app.py) — distinct from the flat admin shape
    # above. Import numbers come from _IMPORT_ADS_DEFAULTS (free=1, gated=2).
    DEFAULT_PUBLIC_CFG = {
        'hand_export': {
            'hand_hard_limit': DEFAULT_CFG['hand_hard_limit'],
            'hand_soft_limit': DEFAULT_CFG['hand_soft_limit'],
            'gate': 'stub_modal',
        },
        'tourney_export': {
            'lifetime_free': DEFAULT_CFG['tourney_lifetime_free'],
            'weekly_limit': DEFAULT_CFG['tourney_weekly_limit'],
            'gate': 'cpx_survey',
        },
        'import': {
            'free': A._IMPORT_ADS_DEFAULTS['free'],
            'gated': A._IMPORT_ADS_DEFAULTS['gated'],
            'total': A._IMPORT_ADS_DEFAULTS['free'] + A._IMPORT_ADS_DEFAULTS['gated'],
            'cadence': 'daily',
            'gate': 'stub_modal',
        },
    }
    DB._d.pop(('config', 'import_ads'), None)

    as_user(None)
    res = CLIENT.get('/api/export-ads-config')
    check('the public config endpoint needs no account', res.status_code == 200,
          str(res.status_code))
    check('and reproduces the nested defaults, reshaped from the admin config',
          res.get_json() == DEFAULT_PUBLIC_CFG, str(res.get_json()))
    check('the admin config endpoint needs an account',
          CLIENT.get('/api/admin/export-ads-config').status_code == 403)

    as_user(FREE_UID)  # not an admin
    check('non-admin cannot read the admin config',
          CLIENT.get('/api/admin/export-ads-config').status_code == 403)
    check('non-admin cannot write the config',
          CLIENT.post('/api/admin/export-ads-config',
                      json={'hand_hard_limit': 10}).status_code == 403)

    as_user('uid-admin')
    res = CLIENT.get('/api/admin/export-ads-config')
    check('admin can read the config', res.status_code == 200, str(res.status_code))
    check('default config matches the public endpoint',
          res.get_json() == DEFAULT_CFG, str(res.get_json()))

    for bad in ({'hand_hard_limit': 'five'}, {'hand_soft_limit': -1},
                {'tourney_hard_limit': True}, {'tourney_soft_limit': 'x'},
                {'tourney_lifetime_free': -1}, {'tourney_weekly_limit': -1},
                {'tourney_lifetime_free': 'one'}, {'tourney_weekly_limit': True},
                {}):
        res = CLIENT.post('/api/admin/export-ads-config', json=bad)
        check(f'rejects malformed body {bad}', res.status_code == 400, str(res.status_code))

    # Reshaped tourney fields: persist independently of the legacy hard/soft
    # pair, and round-trip through both the admin GET and the public GET.
    res = CLIENT.post('/api/admin/export-ads-config',
                      json={'tourney_lifetime_free': 3, 'tourney_weekly_limit': 2})
    check('posting the new tourney fields 200', res.status_code == 200, str(res.status_code))
    check('response reflects the new tourney fields',
          res.get_json()['tourney_lifetime_free'] == 3 and
          res.get_json()['tourney_weekly_limit'] == 2, str(res.get_json()))
    check('legacy tourney hard/soft untouched by the new-field post',
          res.get_json()['tourney_hard_limit'] == DEFAULT_CFG['tourney_hard_limit'] and
          res.get_json()['tourney_soft_limit'] == DEFAULT_CFG['tourney_soft_limit'],
          str(res.get_json()))

    as_user(None)
    res = CLIENT.get('/api/export-ads-config')
    check('public endpoint reflects the saved new tourney fields too, nested',
          res.get_json()['tourney_export']['lifetime_free'] == 3 and
          res.get_json()['tourney_export']['weekly_limit'] == 2, str(res.get_json()))
    check('reshaped tourney block keeps its survey gate label',
          res.get_json()['tourney_export']['gate'] == 'cpx_survey', str(res.get_json()))
    check('hand-export block keeps its shape but reports the stub-modal gate',
          res.get_json()['hand_export'] == {
              'hand_hard_limit': DEFAULT_CFG['hand_hard_limit'],
              'hand_soft_limit': DEFAULT_CFG['hand_soft_limit'],
              'gate': 'stub_modal'}, str(res.get_json()))
    check('import block is present with free/gated/total/cadence/gate',
          res.get_json()['import'] == DEFAULT_PUBLIC_CFG['import'], str(res.get_json()))

    # Admin-configured import numbers flow through to the public import block too.
    as_user('uid-admin')
    res = CLIENT.post('/api/admin/import-ads-config', json={'free': 4, 'gated': 6})
    check('posting new import numbers 200', res.status_code == 200, str(res.status_code))
    as_user(None)
    res = CLIENT.get('/api/export-ads-config')
    check('public import block reflects admin-configured free/gated/total',
          res.get_json()['import'] == {
              'free': 4, 'gated': 6, 'total': 10,
              'cadence': 'daily', 'gate': 'stub_modal'}, str(res.get_json()))
    DB._d.pop(('config', 'import_ads'), None)

    # Reset back to defaults for the rest of the test.
    DB._d.pop(('config', 'export_ads'), None)
    as_user('uid-admin')

    # Bullet 1: hard limit 0 blocks the kind outright — quota_exceeded, no
    # survey ever offered.
    res = CLIENT.post('/api/admin/export-ads-config', json={'hand_hard_limit': 0})
    check('setting hand hard limit to 0 200', res.status_code == 200, str(res.status_code))
    check('other fields untouched', res.get_json()['hand_soft_limit'] == DEFAULT_CFG['hand_soft_limit'])

    as_user(FREE_UID)
    res = _export_hand()
    check('hard limit 0 blocks with quota_exceeded, not survey_required',
          res.status_code == 402 and res.get_json().get('error') == 'quota_exceeded',
          f'{res.status_code} {res.get_json()}')

    # Bullet 3: soft limit 0 (with a positive hard limit) means every slot up
    # to the hard cap is free — no survey ever, but the cap still bites.
    _reset_user(FREE_UID)
    as_user('uid-admin')
    res = CLIENT.post('/api/admin/export-ads-config',
                      json={'hand_hard_limit': 5, 'hand_soft_limit': 0})
    check('soft limit 0 200', res.status_code == 200, str(res.status_code))

    as_user(FREE_UID)
    codes = [_export_hand().status_code for _ in range(5)]
    check('with soft limit 0, all 5 free hand exports need no survey',
          codes == [200] * 5, str(codes))
    res = _export_hand()
    check('the hard cap still applies once soft limit is 0',
          res.status_code == 402 and res.get_json().get('error') == 'quota_exceeded',
          f'{res.status_code} {res.get_json()}')

    # Bullet 4: soft limit > 0 sets how many of the hard-limit slots are
    # survey-gated, counted off the end (free_count = hard - soft).
    _reset_user(FREE_UID)
    as_user('uid-admin')
    res = CLIENT.post('/api/admin/export-ads-config',
                      json={'hand_hard_limit': 5, 'hand_soft_limit': 1})
    check('reconfiguring hand hard/soft limits 200', res.status_code == 200, str(res.status_code))

    as_user(FREE_UID)
    codes = [_export_hand().status_code for _ in range(4)]
    check('hand free_count=5-1=4: first 4 exports need no survey',
          codes == [200] * 4, str(codes))
    res = _export_hand()
    check('hand export 5 needs a stub-modal unlock (the last soft_limit=1 slot)',
          res.status_code == 402 and res.get_json().get('error') == 'survey_required',
          f'{res.status_code} {res.get_json()}')

    # tourney_hard_limit/tourney_soft_limit are no longer read by any gate
    # check — _tourney_export_gate is fully on the lifetime-free + weekly-limit
    # model now (see test_tourney_export_gate for that full lifecycle). This
    # admin-config test only needs to prove those two legacy keys still
    # round-trip through the CRUD surface (asserted above); the new
    # tourney_lifetime_free/tourney_weekly_limit fields are what the gate
    # actually reads, and are exercised end-to-end below.
    DB._d.pop(('config', 'export_ads'), None)
    _reset_user(FREE_UID)
    as_user('uid-admin')
    res = CLIENT.post('/api/admin/export-ads-config',
                      json={'tourney_lifetime_free': 1, 'tourney_weekly_limit': 1})
    check('setting tourney_weekly_limit 200', res.status_code == 200, str(res.status_code))
    as_user(FREE_UID)
    res = _export_tourney()
    check('the lifetime freebie needs no survey even at tourney_weekly_limit=1',
          res.status_code == 200, str(res.status_code))
    res = _export_tourney()
    check('once the lifetime freebie is spent, the next export needs a survey',
          res.status_code == 402 and res.get_json().get('error') == 'survey_required',
          f'{res.status_code} {res.get_json()}')


# ── 4c. Import ads config (admin-controlled free/gated allowance) ───────────
# Nothing enforces these numbers yet (that's a future gate-check task) — this
# only covers the admin-config plumbing: defaults, persistence, and the
# admin-only gate, mirroring test_export_ads_config above.

def test_import_ads_config():
    print('import ads config')
    DB._d.pop(('config', 'import_ads'), None)
    DB.put(('config', 'admins'), {'uids': ['uid-admin']})

    DEFAULT_CFG = {'free': A._IMPORT_ADS_DEFAULTS['free'],
                   'gated': A._IMPORT_ADS_DEFAULTS['gated']}
    check('defaults are 1 free / 2 gated per the feature spec',
          DEFAULT_CFG == {'free': 1, 'gated': 2}, str(DEFAULT_CFG))

    check('the admin config endpoint needs an account',
          CLIENT.get('/api/admin/import-ads-config').status_code == 403)

    as_user(FREE_UID)  # not an admin
    check('non-admin cannot read the admin config',
          CLIENT.get('/api/admin/import-ads-config').status_code == 403)
    check('non-admin cannot write the config',
          CLIENT.post('/api/admin/import-ads-config',
                      json={'free': 10}).status_code == 403)

    as_user('uid-admin')
    res = CLIENT.get('/api/admin/import-ads-config')
    check('admin can read the config', res.status_code == 200, str(res.status_code))
    check('default config matches the hardcoded defaults',
          res.get_json() == DEFAULT_CFG, str(res.get_json()))

    for bad in ({'free': 'one'}, {'gated': -1}, {'free': True}, {}):
        res = CLIENT.post('/api/admin/import-ads-config', json=bad)
        check(f'rejects malformed body {bad}', res.status_code == 400, str(res.status_code))

    res = CLIENT.post('/api/admin/import-ads-config', json={'free': 2})
    check('partial update 200', res.status_code == 200, str(res.status_code))
    check('free updated', res.get_json()['free'] == 2, str(res.get_json()))
    check('gated untouched by a partial update',
          res.get_json()['gated'] == DEFAULT_CFG['gated'], str(res.get_json()))

    res = CLIENT.post('/api/admin/import-ads-config', json={'free': 0, 'gated': 5})
    check('full update 200', res.status_code == 200, str(res.status_code))
    check('both fields persisted', res.get_json() == {'free': 0, 'gated': 5}, str(res.get_json()))

    res = CLIENT.get('/api/admin/import-ads-config')
    check('a fresh GET reflects the persisted config, not the old defaults',
          res.get_json() == {'free': 0, 'gated': 5}, str(res.get_json()))


# ── 5. The 7-day history window ──────────────────────────────────────────────

def test_history_window():
    print('history window')
    for uid, is_pro in ((FREE_UID, False), (PRO_UID, True)):
        _reset_user(uid, is_pro=is_pro)
        _put_tournament(uid, 'trecent', RECENT_TS)
        _put_tournament(uid, 'told', OLD_TS)
        _put_tournament(uid, 'tundated', None)
    _stub_tournament_records({'trecent': _records('trecent'),
                              'told': _records('told'),
                              'tundated': _records('tundated')})

    as_user(FREE_UID)
    body = CLIENT.get('/api/tournaments').get_json()
    ids = {t['tourney_id'] for t in body['tournaments']}
    check('free list hides tournaments older than 7 days', 'told' not in ids, str(ids))
    check('free list keeps recent tournaments', 'trecent' in ids)
    check('a tournament with no timestamp is not assumed old', 'tundated' in ids)
    check('the response says how many were hidden', body['hidden_by_history_cap'] == 1,
          str(body.get('hidden_by_history_cap')))

    res = CLIENT.get('/api/tournaments/told/hands')
    check('free detail view of an expired tournament 404s as history_expired',
          res.status_code == 404 and res.get_json().get('error') == 'history_expired',
          str(res.status_code))
    check('free detail view of a recent tournament still works',
          CLIENT.get('/api/tournaments/trecent/hands').status_code == 200)

    res = _export_tourney('told')
    check('expired tournaments cannot be exported either',
          res.status_code == 404 and res.get_json().get('error') == 'history_expired',
          str(res.status_code))

    as_user(PRO_UID)
    body = CLIENT.get('/api/tournaments').get_json()
    ids = {t['tourney_id'] for t in body['tournaments']}
    check('pro sees the whole history', {'told', 'trecent', 'tundated'} <= ids, str(ids))
    check('pro detail view of an old tournament works',
          CLIENT.get('/api/tournaments/told/hands').status_code == 200)


# ── 6. Imports: quota, window, anonymous sessions, claiming ──────────────────

def _stub_pppoker(records):
    A.fetch_summaries = lambda uid, rdkey, referer: {
        'code': 0, 'I': [r['summary'] for r in records]}
    by_id = {r['summary']['D']: r for r in records}
    A._fetch_record = lambda uid, rdkey, summary, referer: by_id.get(summary.get('D'))


IMPORT_URL = 'https://replay.pppoker.net/?uid=999&rdkey=abc'


def _import():
    return CLIENT.post('/api/analyze', json={'url': IMPORT_URL})


def test_import_quota_and_window():
    print('imports')
    _reset_user(FREE_UID)
    saved = {}
    A._save_tournaments = lambda claims, records, tournaments: (
        saved.update({'tournaments': tournaments}) or (True, {'g1'}))
    _stub_pppoker(_records('trecent'))
    DB._d.pop(('config', 'import_ads'), None)  # defaults: free=1, gated=2 (AC1)

    as_user(FREE_UID)
    res = _import()
    check('import 1 (the free one) needs no unlock', res.status_code == 200,
          str(res.status_code))

    res = _import()
    body = res.get_json()
    check('import 2 (past the free allowance) asks for the gate-stub unlock',
          res.status_code == 402 and body.get('error') == 'survey_required'
          and body.get('kind') == 'import', f'{res.status_code} {body}')

    with A.app.test_request_context('/'):
        A._grant_credit(FREE_UID, 'import')
    res = _import()
    check('a granted import credit unlocks import 2', res.status_code == 200,
          str(res.status_code))
    with A.app.test_request_context('/'):
        check('the credit was spent', A._credits(FREE_UID)['import'] == 0)

    res = _import()
    check('import 3 asks for the gate-stub unlock too (still within gated=2)',
          res.status_code == 402 and res.get_json().get('error') == 'survey_required',
          str(res.status_code))
    with A.app.test_request_context('/'):
        A._grant_credit(FREE_UID, 'import')
    check('import 3 succeeds once unlocked', _import().status_code == 200)

    res = _import()
    body = res.get_json()
    check('import 4 is over the hard cap (free=1 + gated=2), credit or not',
          res.status_code == 402 and body.get('error') == 'quota_exceeded'
          and body.get('kind') == 'import' and body.get('upgrade') is True,
          f'{res.status_code} {body}')

    with A.app.test_request_context('/'):
        import_events = [d for _p, d in DB.items()
                         if len(_p) == 4 and _p[0] == 'users' and _p[1] == FREE_UID
                         and _p[2] == 'gate_events' and d.get('kind') == 'import']
    check('all three successful imports left a gate_events row (AC4)',
          len(import_events) == 3, str(import_events))
    check('only the free one is ungated',
          sum(1 for e in import_events if e['gated'] is False) == 1, str(import_events))

    as_user(None)
    res = _import()
    check('an anonymous import bypasses the gate entirely (nothing to count it against)',
          res.status_code == 200, str(res.status_code))

    as_user(PRO_UID)
    _reset_user(PRO_UID, is_pro=True)
    codes = [_import().status_code for _ in range(5)]
    check('pro imports are uncapped', codes == [200] * 5, str(codes))
    with A.app.test_request_context('/'):
        check('pro imports are not counted', A._quota_state(PRO_UID)['imports'] == 0)


# ── 6b. Gate-stub-modal completion → grants exactly one more action ─────────

def test_stub_completion():
    print('stub completion')
    _reset_user(FREE_UID)
    _put_tournament(FREE_UID, 'trecent', RECENT_TS)
    _stub_tournament_records({'trecent': _records('trecent')})
    saved = {}
    A._save_tournaments = lambda claims, records, tournaments: (
        saved.update({'x': 1}) or (True, {'g1'}))
    _stub_pppoker(_records('trecent'))
    DB._d.pop(('config', 'import_ads'), None)
    DB._d.pop(('config', 'export_ads'), None)

    as_user(None)
    res = CLIENT.post('/api/gate/stub-completion',
                      json={'kind': 'import', 'completion_id': 'c0'})
    check('stub completion needs an account', res.status_code == 401, str(res.status_code))
    as_user(FREE_UID)

    res = CLIENT.post('/api/gate/stub-completion', json={'kind': 'nope', 'completion_id': 'c1'})
    check('an unknown kind is rejected', res.status_code == 400, str(res.status_code))
    res = CLIENT.post('/api/gate/stub-completion', json={'kind': 'import'})
    check('a missing completion_id is rejected', res.status_code == 400, str(res.status_code))

    # Use up the free import allowance (free=1) so the next one is gated.
    check('the free import goes through untouched', _import().status_code == 200)
    res = _import()
    check('the next import is gated', res.status_code == 402
          and res.get_json().get('error') == 'survey_required', str(res.status_code))

    res = CLIENT.post('/api/gate/stub-completion',
                      json={'kind': 'import', 'completion_id': 'stub-import-1'})
    check('a verified stub completion is recorded', res.status_code == 200
          and res.get_json().get('ok') is True, str(res.get_json()))
    with A.app.test_request_context('/'):
        check('and grants exactly one import credit', A._credits(FREE_UID)['import'] == 1)

    check('the gated import now succeeds — exactly one more action unlocked',
          _import().status_code == 200)
    res = _import()
    check('a further import is gated again — the credit was single-use',
          res.status_code == 402 and res.get_json().get('error') == 'survey_required',
          str(res.status_code))

    # Replaying the same completion_id must not grant a second credit.
    res = CLIENT.post('/api/gate/stub-completion',
                      json={'kind': 'import', 'completion_id': 'stub-import-1'})
    check('a replayed completion_id is reported as already_recorded',
          res.status_code == 200 and res.get_json().get('already_recorded') is True,
          str(res.get_json()))
    with A.app.test_request_context('/'):
        check('and grants no additional credit', A._credits(FREE_UID)['import'] == 0)

    # Same contract for hand exports, which is the kind that actually switched
    # away from CPX (AC2).
    codes = [_export_hand().status_code for _ in range(2)]
    check('the two free hand exports need no unlock', codes == [200, 200], str(codes))
    res = _export_hand()
    check('hand export 3 is gated', res.status_code == 402
          and res.get_json().get('error') == 'survey_required', str(res.status_code))
    CLIENT.post('/api/gate/stub-completion',
               json={'kind': 'hand_export', 'completion_id': 'stub-hand-1'})
    check('the gate-stub completion unlocks exactly hand export 3',
          _export_hand().status_code == 200)
    res = _export_hand()
    check('hand export 4 is gated again', res.status_code == 402
          and res.get_json().get('error') == 'survey_required', str(res.status_code))

    # AC6: with the stub modal disabled server-side, completion is refused
    # outright rather than silently granting a credit for an ad never shown.
    A._GATE_STUB_MODAL_ENABLED = False
    try:
        res = CLIENT.post('/api/gate/stub-completion',
                          json={'kind': 'import', 'completion_id': 'stub-import-disabled'})
        check('a disabled stub modal refuses the completion endpoint',
              res.status_code == 503, str(res.status_code))
    finally:
        A._GATE_STUB_MODAL_ENABLED = True


def test_free_import_prunes_old_tournaments():
    print('import history window')
    _reset_user(FREE_UID)
    kept = {}
    A._save_tournaments = lambda claims, records, tournaments: (
        kept.update({'ids': [t['tourney_id'] for t in tournaments]}) or (True, set()))

    old, new = _records('told', 2), _records('tfresh', 2)
    _stub_pppoker(old + new)
    from hand_parser import extract_tourney_id
    real_process = A.process_hands
    A.process_hands = lambda records: (
        [], [],
        {},
        [{'tourney_id': tid, 'earliest_ts': ts, 'room_name': 'DEEP FREEZE'}
         for tid, ts in (('told', OLD_TS), ('tfresh', RECENT_TS))
         if any(extract_tourney_id(r['summary']['D']) == tid for r in records)])
    A.validate_hands = lambda records: {}
    try:
        as_user(FREE_UID)
        body = _import().get_json()
    finally:
        A.process_hands = real_process

    check('the out-of-window tournament is not persisted',
          kept.get('ids') == ['tfresh'], str(kept))
    check('and it is pruned from the response',
          [t['tourney_id'] for t in body['tournaments']] == ['tfresh'],
          str(body['tournaments']))
    check('the response reports the prune', body['history_expired_tournaments'] == 1,
          str(body.get('history_expired_tournaments')))


def test_anon_import_and_claim():
    print('anonymous import and claim')
    _reset_user(FREE_UID)
    BUCKET.objects.clear()
    # Mirrors the real thing: an import with no verified token persists nothing.
    A._save_tournaments = lambda claims, records, tournaments: (
        (True, {'g1'}) if claims else (False, set()))
    A._tournament_graphs = lambda records, tournaments: [{'tourney_id': 'stub'}]
    _stub_pppoker(_records('trecent'))

    as_user(None)
    body = _import().get_json()
    token = body.get('session_token')
    check('an anonymous import returns a signed session token', bool(token))
    check('and per-tournament graph data, so the graphs render without an account',
          body.get('tournament_graphs') == [{'tourney_id': 'stub'}])
    check('the anonymous import is parked in Storage, not the history',
          any(k.startswith('anon_sessions/') for k in BUCKET.objects), str(BUCKET.objects))
    check('anonymous imports are reported as unsaved', body.get('saved') is False)

    check('a forged session token loads nothing',
          A._load_anon_session(token.split('.')[0] + '.forged') is None)

    as_user(FREE_UID)
    res = CLIENT.post('/api/analyze/claim', json={'session_token': token})
    body = res.get_json()
    check('signing in claims the pending import',
          res.status_code == 200 and body.get('claimed') is True, f'{res.status_code} {body}')
    with A.app.test_request_context('/'):
        check('the claim counts as the day\'s import',
              A._quota_state(FREE_UID)['imports'] == 1)
    check('the claimed blob is cleaned up', not BUCKET.objects, str(BUCKET.objects))

    res = CLIENT.post('/api/analyze/claim', json={'session_token': token})
    check('a claimed token cannot be replayed', res.status_code == 404, str(res.status_code))

    # At the import cap the blob must survive so it can be claimed tomorrow.
    BUCKET.objects.clear()
    as_user(None)
    token = _import().get_json()['session_token']
    DB.put(('users', FREE_UID), {'is_pro': False,
                                 'quota': {'day': A._utc_day(), 'imports': 3,
                                           'hand_exports': 0, 'tourney_exports': 0}})
    as_user(FREE_UID)
    res = CLIENT.post('/api/analyze/claim', json={'session_token': token})
    check('claiming over the import cap is refused',
          res.status_code == 402 and res.get_json().get('error') == 'quota_exceeded',
          str(res.status_code))
    check('and the pending import is kept for a later attempt',
          any(k.startswith('anon_sessions/') for k in BUCKET.objects))

    as_user(None)
    res = CLIENT.post('/api/analyze/claim', json={'session_token': token})
    check('claiming needs an account', res.status_code == 401, str(res.status_code))


# ── 7. Survey callbacks ──────────────────────────────────────────────────────

def test_cpx_postback():
    print('cpx postback')
    _reset_user(FREE_UID)
    trans = 'trans-1'
    good  = hashlib.md5(f'{trans}-cpx-secret'.encode()).hexdigest()

    res = CLIENT.get(f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans}'
                     f'&subid_1=hand&status=1&hash=nope')
    check('a bad hash is rejected', res.status_code == 403, str(res.status_code))
    with A.app.test_request_context('/'):
        check('and grants nothing', A._credits(FREE_UID)['hand'] == 0)

    unhyphenated = hashlib.md5(f'{trans}cpx-secret'.encode()).hexdigest()
    res = CLIENT.get(f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans}'
                     f'&subid_1=hand&status=1&hash={unhyphenated}')
    check('a hash missing the hyphen separator is rejected',
          res.status_code == 403, str(res.status_code))
    with A.app.test_request_context('/'):
        check('and grants nothing', A._credits(FREE_UID)['hand'] == 0)

    url = (f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans}&subid_1=hand'
           f'&status=1&amount_usd=0.35&offer_id=42&hash={good}')
    res = CLIENT.get(url)
    check('a valid postback answers a literal 1',
          res.status_code == 200 and res.get_data(as_text=True) == '1',
          res.get_data(as_text=True))
    with A.app.test_request_context('/'):
        check('and grants exactly one credit', A._credits(FREE_UID)['hand'] == 1)
    check('the completion is recorded with the provider payload',
          (DB.get(('users', FREE_UID, 'survey_completions', trans)) or {}).get('offer_id') == '42',
          str(DB.get(('users', FREE_UID, 'survey_completions', trans))))

    CLIENT.get(url)
    CLIENT.get(url)
    with A.app.test_request_context('/'):
        check('redelivery of the same trans_id pays once',
              A._credits(FREE_UID)['hand'] == 1)

    rev = (f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans}&subid_1=hand'
           f'&status=2&hash={good}')
    CLIENT.get(rev)
    with A.app.test_request_context('/'):
        check('a reversal claws back the unspent credit',
              A._credits(FREE_UID)['hand'] == 0)
    CLIENT.get(rev)
    with A.app.test_request_context('/'):
        check('a repeated reversal does not go negative',
              A._credits(FREE_UID)['hand'] == 0)

    # A credit that has already been spent has nothing left to reverse.
    trans2 = 'trans-2'
    hash2  = hashlib.md5(f'{trans2}-cpx-secret'.encode()).hexdigest()
    CLIENT.get(f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans2}'
               f'&subid_1=tourney&status=1&hash={hash2}')
    with A.app.test_request_context('/'):
        A._consume_credit(FREE_UID, 'tourney')
    CLIENT.get(f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans2}'
               f'&subid_1=tourney&status=2&hash={hash2}')
    with A.app.test_request_context('/'):
        check('reversing a spent credit leaves the balance alone',
              A._credits(FREE_UID)['tourney'] == 0)

    # Non-complete event types (screen-outs, bonuses) still answer '1' so CPX
    # doesn't retry, but must not grant a credit.
    for ev_type in ('out', 'bonus'):
        trans_ev = f'trans-{ev_type}'
        hash_ev  = hashlib.md5(f'{trans_ev}-cpx-secret'.encode()).hexdigest()
        res = CLIENT.get(f'/api/cpx/postback?user_id={FREE_UID}&trans_id={trans_ev}'
                         f'&subid_1=hand&status=1&type={ev_type}&hash={hash_ev}')
        check(f'type={ev_type} is accepted',
              res.status_code == 200 and res.get_data(as_text=True) == '1',
              res.get_data(as_text=True))
        with A.app.test_request_context('/'):
            check(f'but type={ev_type} grants zero credits',
                  A._credits(FREE_UID)['hand'] == 0)


def test_survey_config_hash():
    print('survey config hash')
    as_user(FREE_UID)
    res = CLIENT.get('/api/survey-config')
    check('survey-config responds ok', res.status_code == 200, str(res.status_code))
    expected = hashlib.md5(f'{FREE_UID}-cpx-secret'.encode()).hexdigest()
    check('secure_hash is md5(uid + "-" + secret)',
          res.get_json()['cpx']['secure_hash'] == expected,
          res.get_json()['cpx'])


def test_tally_callback():
    print('tally callback')
    _reset_user(FREE_UID)
    body = json.dumps({'eventType': 'FORM_RESPONSE', 'data': {
        'responseId': 'resp-1', 'formId': 'form-1',
        'fields': [{'key': 'uid', 'label': 'uid', 'value': FREE_UID},
                   {'key': 'kind', 'label': 'kind', 'value': 'tourney'}]}})
    sig = base64.b64encode(hmac.new(b'tally-secret', body.encode(),
                                    hashlib.sha256).digest()).decode()

    res = CLIENT.post('/api/tally/callback', data=body,
                      headers={'Content-Type': 'application/json',
                               'Tally-Signature': 'wrong'})
    check('an unsigned submission is rejected', res.status_code == 403, str(res.status_code))

    res = CLIENT.post('/api/tally/callback', data=body,
                      headers={'Content-Type': 'application/json',
                               'Tally-Signature': sig})
    check('a signed submission is accepted', res.status_code == 200, str(res.status_code))
    with A.app.test_request_context('/'):
        check('and grants the credit named by the hidden field',
              A._credits(FREE_UID)['tourney'] == 1)

    res = CLIENT.post('/api/tally/callback', data=body,
                      headers={'Content-Type': 'application/json',
                               'Tally-Signature': sig})
    check('a redelivered responseId pays once',
          res.get_json().get('duplicate') is True, str(res.get_json()))
    with A.app.test_request_context('/'):
        check('so the balance is unchanged', A._credits(FREE_UID)['tourney'] == 1)


def test_credit_endpoints():
    print('credit endpoints')
    _reset_user(FREE_UID)
    as_user(None)
    check('credits need an account', CLIENT.get('/api/credits').status_code == 401)
    check('ad tokens need an account',
          CLIENT.post('/api/ad-token', json={'kind': 'hand'}).status_code == 401)

    as_user(FREE_UID)
    check('credits start empty',
          CLIENT.get('/api/credits').get_json() == {'hand': 0, 'tourney': 0, 'import': 0})
    res = CLIENT.post('/api/ad-token', json={'kind': 'hand'})
    check('no credit means no token',
          res.status_code == 402 and res.get_json().get('error') == 'survey_required',
          str(res.status_code))

    with A.app.test_request_context('/'):
        A._grant_credit(FREE_UID, 'hand')
    check('the client can poll for the granted credit',
          CLIENT.get('/api/credits').get_json()['hand'] == 1)
    res = CLIENT.post('/api/ad-token', json={'kind': 'hand'})
    check('a credit buys a token', res.status_code == 200 and res.get_json().get('token'))
    check('and is spent doing so', CLIENT.get('/api/credits').get_json()['hand'] == 0)
    check('an unknown kind is rejected',
          CLIENT.post('/api/ad-token', json={'kind': 'wat'}).status_code == 400)


def main():
    for test in (test_quota, test_credits, test_tourney_export_state, test_gate_events,
                 test_ad_tokens, test_export_gates,
                 test_export_ads_config, test_import_ads_config,
                 test_history_window, test_import_quota_and_window, test_stub_completion,
                 test_free_import_prunes_old_tournaments, test_anon_import_and_claim,
                 test_cpx_postback, test_survey_config_hash, test_tally_callback,
                 test_credit_endpoints):
        test()
    print()
    if _FAILURES:
        print(f'tiering: FAIL ({len(_FAILURES)}) — ' + '; '.join(_FAILURES))
        sys.exit(1)
    print('tiering: PASS')


if __name__ == '__main__':
    main()
