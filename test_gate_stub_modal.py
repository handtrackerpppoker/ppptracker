"""
test_gate_stub_modal.py — end-to-end shape test for POST /api/gate/stub-completion
against a fake Firestore, so the handler body runs without credentials or
network.

This is the server half of the "watch to unlock" stub modal (see
_showGateStubModal in static/app.js): a self-hosted stand-in for a real
rewarded-video ad while ayeT-Studios/Wannads publisher approvals are pending.
The endpoint does exactly one thing — record a completion — so the interesting
cases are the guardrails around that: auth required, kind validated, and
idempotent on completion_id so a double-clicked OK button (or a retried
request) doesn't record twice.

    python test_gate_stub_modal.py
"""

import json
import os
import sys

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')

UID = 'uid-gate-stub'


# ── Fake Firestore ───────────────────────────────────────────────────────────
# Just enough surface for _user_ref(uid).collection('gate_events').document(id):
# get/create, matching the create()-based idempotency pattern used elsewhere
# for survey_completions/ad_jtis (see test_tiering.py).

class _Snap:
    def __init__(self, doc_id, data):
        self.id, self._data, self.exists = doc_id, data, data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def collection(self, name):
        return _Col(self._store, self._path + (name,))

    def get(self, transaction=None):
        return _Snap(self._path[-1], self._store.get(self._path))

    def create(self, data):
        from google.api_core import exceptions as gexc
        if self._store.get(self._path) is not None:
            raise gexc.AlreadyExists(f'{self._path} exists')
        self._store.put(self._path, dict(data))

    def set(self, data, merge=False):
        cur = dict(self._store.get(self._path) or {}) if merge else {}
        cur.update(data)
        self._store.put(self._path, cur)

    def update(self, data):
        cur = dict(self._store.get(self._path) or {})
        cur.update(data)
        self._store.put(self._path, cur)


class _Col:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def document(self, doc_id):
        return _Doc(self._store, self._path + (doc_id,))


class _Txn:
    """Applies straight through, same as test_tiering.py's fake — single-
    threaded, so there's nothing to actually retry."""

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


def _json(res):
    body = res.get_data(as_text=True)
    try:
        return res.status_code, json.loads(body)
    except ValueError:
        raise AssertionError('non-JSON response (%s): %s'
                             % (res.status_code, body[:300]))


def main():
    import google.cloud.firestore as _gcf
    _gcf.transactional = lambda fn: fn   # the fake transaction needs no retry loop

    import app as A

    db = FakeDB()
    A._get_admin_db = lambda: db

    caller = {'uid': UID}
    A._verify_bearer = lambda req: caller['uid']

    client = A.app.test_client()
    problems = []

    def check(label, cond, detail=''):
        if not cond:
            problems.append(label + (' — ' + detail if detail else ''))

    def post(body, headers=None):
        return _json(client.post('/api/gate/stub-completion',
                                 data=json.dumps(body) if body is not None else 'not json',
                                 content_type='application/json',
                                 headers=headers or {}))

    def event(completion_id):
        return db.get(('users', UID, 'gate_events', completion_id))

    # ── 1. Auth required ────────────────────────────────────────────────────
    caller['uid'] = None
    status, body = post({'kind': 'import', 'ts': 1, 'completion_id': 'c-anon'})
    check('signed-out 401', status == 401, str(status))
    check('signed-out error code', body.get('error') == 'login_required', str(body))
    check('signed-out did not write', event('c-anon') is None)
    caller['uid'] = UID

    # ── 2. kind validation ──────────────────────────────────────────────────
    status, body = post({'kind': 'tourney', 'ts': 1, 'completion_id': 'c-bad-kind'})
    check('bad kind 400', status == 400, str(status))
    check('bad kind error names the field', 'kind' in body.get('error', ''), str(body))
    check('bad kind did not write', event('c-bad-kind') is None)

    status, _ = post({'ts': 1, 'completion_id': 'c-no-kind'})
    check('missing kind 400', status == 400, str(status))

    # ── 3. completion_id required ───────────────────────────────────────────
    status, body = post({'kind': 'import', 'ts': 1})
    check('missing completion_id 400', status == 400, str(status))
    check('missing completion_id error names the field',
          'completion_id' in body.get('error', ''), str(body))

    # ── 4. Happy path — import ──────────────────────────────────────────────
    status, body = post({'kind': 'import', 'ts': 1700000000, 'completion_id': 'c-import-1'})
    check('happy path 200', status == 200, str(status))
    check('happy path ok', body.get('ok') is True, str(body))
    check('happy path echoes kind', body.get('kind') == 'import', str(body))
    check('happy path not already_recorded', body.get('already_recorded') is False, str(body))

    rec = event('c-import-1')
    check('event written', rec is not None)
    check('event kind', (rec or {}).get('kind') == 'import', str(rec))
    check('event gated true', (rec or {}).get('gated') is True, str(rec))
    check('event provider is stub', (rec or {}).get('gate_provider') == 'stub', str(rec))
    check('event completion id mirrors doc id',
          (rec or {}).get('gate_completion_id') == 'c-import-1', str(rec))
    from google.cloud import firestore as _gcf
    check('event carries a server-resolved "at" timestamp',
          (rec or {}).get('at') == _gcf.SERVER_TIMESTAMP, str(rec))

    # ── 5. Happy path — hand_export ─────────────────────────────────────────
    status, body = post({'kind': 'hand_export', 'ts': 1, 'completion_id': 'c-hand-1'})
    check('hand_export 200', status == 200, str(status))
    check('hand_export event kind', (event('c-hand-1') or {}).get('kind') == 'hand_export')

    # ── 6. Idempotent on completion_id (double-click / retry) ──────────────
    status, body = post({'kind': 'import', 'ts': 1700000999, 'completion_id': 'c-import-1'})
    check('duplicate completion 200 (not an error)', status == 200, str(status))
    check('duplicate completion flagged', body.get('already_recorded') is True, str(body))
    # The original record must survive untouched — a duplicate delivery must
    # not silently rewrite "at" or anything else on the first grant.
    check('duplicate did not overwrite original event',
          (event('c-import-1') or {}).get('at') == rec.get('at'),
          str(event('c-import-1')))

    # A double-click with a *different* completion_id (client failed to reuse
    # the id it generated at modal-open) is out of this endpoint's control —
    # confirm it is treated as two independent, valid completions rather than
    # erroring, since the client-side guard (see _gateStubOkClicked in
    # static/app.js) is what's actually responsible for reusing one id per
    # modal open.
    status, body = post({'kind': 'import', 'ts': 2, 'completion_id': 'c-import-2'})
    check('second distinct id also recorded', status == 200 and body.get('already_recorded') is False,
          str((status, body)))

    for p in problems:
        print('  FAIL', p)
    print('gate stub completion API: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
