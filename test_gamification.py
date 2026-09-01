"""
test_gamification.py — the points/badge economy, rule by rule.

Most of this file drives the pure core, `gamification.evaluate(state, hands, ts, tz)`, with
a hand-built clock. That is the whole reason the scoring logic takes its time as an argument
instead of reading it: rules like "the 4th import inside 60 minutes" or "exactly 7 silent
days" are otherwise untestable without sleeping.

The last section exercises the Firestore shell (on_import / settle_week / snapshot) against
the same fake-Firestore pattern used by test_admin_users.py and test_leaks_api.py, extended
with order_by/limit and a transaction stub.

    python test_gamification.py
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'test-bucket')

import gamification as G

ADL = ZoneInfo('Australia/Adelaide')
UTC = ZoneInfo('UTC')

problems = []


def check(label, cond, detail=''):
    if not cond:
        problems.append('%s %s' % (label, ('— ' + detail) if detail else ''))


def ts(y, m, d, hh=0, mm=0, tz=ADL):
    """Epoch seconds for a wall-clock time in `tz`."""
    return int(datetime(y, m, d, hh, mm, tzinfo=tz).timestamp())


def run(state, hands, when, tz=ADL):
    return G.evaluate(state, hands, when, tz)


def codes(result):
    return [b['code'] for b in result['badges']]


def award_codes(result):
    return [a['code'] for a in result['awards']]


# ── 1. Anti-spam floor ───────────────────────────────────────────────────────

def test_min_hands():
    st, r = run({}, 14, ts(2026, 3, 2, 12, 0))
    check('14 hands scores nothing', r['points'] == 0, str(r['points']))
    check('14 hands is flagged skipped', r['skipped'] == 'min_hands', str(r['skipped']))
    check('14 hands earns no badge', not r['badges'], str(r['badges']))
    check('14 hands leaves lifetime untouched', st['lifetime_hands'] == 0,
          str(st['lifetime_hands']))
    check('14 hands is not logged in the window', not st['recent_imports'],
          str(st['recent_imports']))
    check('14 hands does not start a streak', st['streak_days'] == 0, str(st['streak_days']))

    st, r = run({}, 15, ts(2026, 3, 2, 12, 0))
    # Kickoff 50 + day-1 streak 10. 15 hands is below the 30-hand band, so no milestone.
    check('15 hands scores kickoff + streak', r['points'] == 60, str(r['points']))
    check('exactly 15 hands earns The Robbi', 'J4' in codes(r), str(codes(r)))


# ── 2. Daily points ──────────────────────────────────────────────────────────

def test_kickoff_then_base():
    st, r1 = run({}, 20, ts(2026, 3, 2, 12, 0))
    check('first import is the kickoff', 'kickoff' in award_codes(r1), str(award_codes(r1)))

    st, r2 = run(st, 20, ts(2026, 3, 2, 13, 0))
    check('second import is a base import', 'base' in award_codes(r2), str(award_codes(r2)))
    check('second import is not another kickoff', 'kickoff' not in award_codes(r2),
          str(award_codes(r2)))
    base = [a for a in r2['awards'] if a['code'] == 'base']
    check('base import pays 10', base and base[0]['points'] == 10, str(r2['awards']))
    check('day imports accumulate', st['day_imports'] == 2, str(st['day_imports']))


def test_streak_ramp_and_cap():
    st = {}
    seen = []
    for day in range(1, 13):
        st, r = run(st, 20, ts(2026, 3, day, 12, 0))
        streak = [a for a in r['awards'] if a['code'] == 'streak']
        seen.append(streak[0]['points'] if streak else 0)
    check('day 1 streak pays 10', seen[0] == 10, str(seen[0]))
    check('day 3 streak pays 30', seen[2] == 30, str(seen[2]))
    check('day 10 streak pays 100', seen[9] == 100, str(seen[9]))
    check('day 12 streak stays capped at 100', seen[11] == 100, str(seen[11]))
    check('streak counter reached 12', st['streak_days'] == 12, str(st['streak_days']))
    check('7-day streak earned the Royal Flush', 'ROYAL' in st['badge_codes'],
          str(st['badge_codes']))


def test_streak_only_once_per_day():
    st, _ = run({}, 20, ts(2026, 3, 2, 12, 0))
    st, r = run(st, 20, ts(2026, 3, 2, 18, 0))
    check('streak bonus does not repeat within a day',
          'streak' not in award_codes(r), str(award_codes(r)))


def test_break_window():
    # The break follows the tournament clock (top of the UTC hour), not the configured zone.
    _, r = run({}, 20, ts(2026, 3, 2, 12, 54, tz=UTC))
    check('UTC :54 is outside the break window', 'break_sync' not in award_codes(r),
          str(award_codes(r)))
    _, r = run({}, 20, ts(2026, 3, 2, 12, 55, tz=UTC))
    check('UTC :55 is inside the break window', 'break_sync' in award_codes(r),
          str(award_codes(r)))
    _, r = run({}, 20, ts(2026, 3, 2, 12, 59, tz=UTC))
    check('UTC :59 is inside the break window', 'break_sync' in award_codes(r),
          str(award_codes(r)))

    # Adelaide is UTC+X:30, so the window is a half-hour off local time. The real break at
    # UTC :55 is Adelaide-local :25; the bug was crediting it at Adelaide-local :55 (UTC :25).
    _, r = run({}, 20, ts(2026, 3, 2, 12, 25, tz=ADL))       # == UTC :55
    check('Adelaide :25 (UTC :55) is inside the break window',
          'break_sync' in award_codes(r), str(award_codes(r)))
    _, r = run({}, 20, ts(2026, 3, 2, 12, 55, tz=ADL))       # == UTC :25
    check('Adelaide :55 (UTC :25) is NOT the break window',
          'break_sync' not in award_codes(r), str(award_codes(r)))


def test_hourly_surge():
    st = {}
    results = []
    for i, minute in enumerate((0, 10, 20, 30, 40)):
        st, r = run(st, 20, ts(2026, 3, 2, 12, minute))
        results.append(r)
    check('surge does not fire on the 3rd import',
          'surge' not in award_codes(results[2]), str(award_codes(results[2])))
    check('surge fires on the 4th import in the hour',
          'surge' in award_codes(results[3]), str(award_codes(results[3])))
    check('the 4th import also earns The Wheel', 'A2345' in codes(results[3]),
          str(codes(results[3])))
    check('surge does not fire again on the 5th',
          'surge' not in award_codes(results[4]), str(award_codes(results[4])))

    # Window drains — one lone import an hour later re-arms it without paying.
    st, r = run(st, 20, ts(2026, 3, 2, 14, 0))
    check('a drained window re-arms without paying', 'surge' not in award_codes(r),
          str(award_codes(r)))
    check('surge is armed again', st['surge_armed'] is True, str(st['surge_armed']))
    for minute in (10, 20, 30):
        st, r = run(st, 20, ts(2026, 3, 2, 14, minute))
    check('surge fires again once 4 land in the new window',
          'surge' in award_codes(r), str(award_codes(r)))


def test_volume_milestones_stack():
    st, r = run({}, 900, ts(2026, 3, 2, 12, 0))
    paid = {a['code'] for a in r['awards']}
    check('30-hand band paid', 'milestone_30' in paid, str(paid))
    check('200-hand band paid', 'milestone_200' in paid, str(paid))
    check('750-hand band paid', 'milestone_750' in paid, str(paid))
    check('2000-hand band not paid', 'milestone_2000' not in paid, str(paid))
    milestone_points = sum(a['points'] for a in r['awards'] if a['code'].startswith('milestone'))
    check('900 hands stacks to 220 milestone points', milestone_points == 220,
          str(milestone_points))
    check('900-hand day totals 280', r['points'] == 280, str(r['points']))

    st, r2 = run(st, 100, ts(2026, 3, 2, 13, 0))
    check('crossed bands do not pay twice',
          not [a for a in r2['awards'] if a['code'].startswith('milestone')],
          str(award_codes(r2)))

    st, r3 = run(st, 1200, ts(2026, 3, 2, 14, 0))   # day total now 2200
    check('2000-hand band pays when reached later the same day',
          'milestone_2000' in award_codes(r3), str(award_codes(r3)))


def test_5k_increments():
    st, r = run({}, 5200, ts(2026, 3, 2, 12, 0))
    five_k = [a for a in r['awards'] if a['code'] == 'milestone_5k']
    check('5k increment pays 1000', five_k and five_k[0]['points'] == 1000, str(five_k))

    st, r = run(st, 4900, ts(2026, 3, 2, 13, 0))     # day total 10,100
    five_k = [a for a in r['awards'] if a['code'] == 'milestone_5k']
    check('second 5k increment pays another 1000',
          five_k and five_k[0]['points'] == 1000, str(five_k))

    # One huge import that crosses two increments at once pays for both.
    st2, r2 = run({}, 11000, ts(2026, 3, 3, 12, 0))
    five_k = [a for a in r2['awards'] if a['code'] == 'milestone_5k']
    check('a single import crossing two increments pays 2000',
          five_k and five_k[0]['points'] == 2000, str(five_k))


# ── 3. Timezone behaviour ────────────────────────────────────────────────────

def test_day_boundary_is_local_not_utc():
    # 09:00 and 23:00 Adelaide are the same local day but *different* UTC days.
    early = ts(2026, 8, 14, 9, 0)
    late  = ts(2026, 8, 14, 23, 0)
    check('the two stamps really do straddle a UTC day',
          datetime.fromtimestamp(early, UTC).day != datetime.fromtimestamp(late, UTC).day)

    st, _ = run({}, 20, early)
    st, r = run(st, 20, late)
    check('same local day is not a second kickoff', 'kickoff' not in award_codes(r),
          str(award_codes(r)))

    # The same two stamps under a UTC configuration must roll the day over.
    st, _ = run({}, 20, early, tz=UTC)
    st, r = run(st, 20, late, tz=UTC)
    check('under UTC the same stamps do roll the day', 'kickoff' in award_codes(r),
          str(award_codes(r)))


def test_steel_wheel_follows_configured_zone():
    when = ts(2026, 3, 2, 3, 0)          # 3am Adelaide
    _, r = run({}, 20, when)
    check('3am local earns the Steel Wheel', 'STEEL' in codes(r), str(codes(r)))

    _, r = run({}, 20, when, tz=UTC)     # same instant, 17:30 UTC the day before
    check('the same instant is not 2-5am in UTC', 'STEEL' not in codes(r), str(codes(r)))

    _, r = run({}, 20, ts(2026, 3, 2, 5, 0))
    check('5am is outside the window', 'STEEL' not in codes(r), str(codes(r)))


def test_closer():
    # The Closer is day-boundary relative (local 23:55->midnight), so it stays in the configured
    # zone — unlike the break window, which now tracks the UTC hour. At Adelaide 23:57 (UTC 13:27)
    # the Closer fires but the break window does not: the two are decoupled.
    _, r = run({}, 20, ts(2026, 3, 2, 23, 57))
    check('23:57 earns The Closer', '99' in codes(r), str(codes(r)))
    check('Adelaide 23:57 no longer pays break-time sync', 'break_sync' not in award_codes(r),
          str(award_codes(r)))
    _, r = run({}, 20, ts(2026, 3, 2, 23, 54))
    check('23:54 is too early for The Closer', '99' not in codes(r), str(codes(r)))


# ── 4. Behavioural badges ────────────────────────────────────────────────────

def test_break_master():
    st = {}
    for day in range(1, 6):
        st, r = run(st, 20, ts(2026, 3, day, 12, 25))   # Adelaide :25 == UTC :55, in-break
        if day < 5:
            check('K-9 not earned on break day %d' % day, 'K9' not in codes(r), str(codes(r)))
    check('K-9 earned on the 5th consecutive break day', 'K9' in st['badge_codes'],
          str(st['badge_codes']))


def test_break_master_resets_on_a_gap():
    st = {}
    for day in (1, 2, 3):
        st, _ = run(st, 20, ts(2026, 3, day, 12, 25))   # Adelaide :25 == UTC :55, in-break
    st, _ = run(st, 20, ts(2026, 3, 6, 12, 25))     # gap — break streak restarts
    check('a missed break day restarts the count', st['break_streak_days'] == 1,
          str(st['break_streak_days']))


def test_the_machine():
    st = {}
    r = None
    for i in range(10):
        st, r = run(st, 20, ts(2026, 3, 2, 8, 0) + i * 1800)
    check('10 imports in 24h earns The Machine', 'Q7' in st['badge_codes'],
          str(st['badge_codes']))
    check('The Machine lands on the 10th import', 'Q7' in codes(r), str(codes(r)))


def test_downswing():
    st = {}
    for day in range(1, 11):
        st, _ = run(st, 20, ts(2026, 3, day, 12, 0))
    check('streak reached 10', st['streak_days'] == 10, str(st['streak_days']))
    st, r = run(st, 20, ts(2026, 3, 13, 12, 0))     # two days missed
    check('breaking a 10-day streak earns 2-3 Offsuit', '23o' in codes(r), str(codes(r)))
    check('the streak restarts at 1', st['streak_days'] == 1, str(st['streak_days']))

    # A shorter streak breaking earns nothing.
    st = {}
    for day in range(1, 4):
        st, _ = run(st, 20, ts(2026, 3, day, 12, 0))
    st, r = run(st, 20, ts(2026, 3, 8, 12, 0))
    check('breaking a 3-day streak earns nothing', '23o' not in codes(r), str(codes(r)))


def test_ghost():
    base = {}
    base, _ = run(base, 20, ts(2026, 3, 1, 12, 0))

    st, r = run(base, 20, ts(2026, 3, 9, 12, 0))    # 8-day gap == 7 full silent days
    check('exactly 7 silent days earns The Ghost', '83' in codes(r), str(codes(r)))

    st, r = run(base, 20, ts(2026, 3, 8, 12, 0))    # 6 silent days
    check('6 silent days earns nothing', '83' not in codes(r), str(codes(r)))

    st, r = run(base, 20, ts(2026, 3, 10, 12, 0))   # 8 silent days
    check('8 silent days earns nothing', '83' not in codes(r), str(codes(r)))


def test_volume_badges_and_idempotency():
    st, r = run({}, 1000, ts(2026, 3, 2, 12, 0))
    check('1,000 lifetime hands earns 7-2 Offsuit', '72o' in codes(r), str(codes(r)))

    st, r = run(st, 1000, ts(2026, 3, 3, 12, 0))
    check('7-2 Offsuit is not awarded twice', '72o' not in codes(r), str(codes(r)))
    check('it is still held', '72o' in st['badge_codes'], str(st['badge_codes']))

    st, r = run(st, 9000, ts(2026, 3, 4, 12, 0))    # lifetime 11,000
    check('10,000 lifetime hands earns Pocket Fours', '44' in codes(r), str(codes(r)))

    # A single enormous import crosses several thresholds and earns each of them.
    _, r = run({}, 30000, ts(2026, 3, 2, 12, 0))
    check('one big import earns every threshold crossed',
          {'72o', '44', 'JJ'}.issubset(set(codes(r))), str(codes(r)))


# ── 5. Weekly personal track ─────────────────────────────────────────────────

def test_pb_and_target_and_comeback():
    # Week 1 (Mon 2 Mar 2026): 1,000 hands over one import.
    st, _ = run({}, 1000, ts(2026, 3, 2, 12, 0))
    check('week 1 recorded', st['week_hands'] == 1000, str(st['week_hands']))

    # Week 2: first import rolls the week and banks week 1 as history.
    st, r = run(st, 900, ts(2026, 3, 9, 12, 0))
    check('previous week volume carried over', st['prev_week_hands'] == 1000,
          str(st['prev_week_hands']))
    check('PB not paid while still behind', 'pb' not in award_codes(r), str(award_codes(r)))
    check('target not paid while still behind', 'weekly_target' not in award_codes(r),
          str(award_codes(r)))

    st, r = run(st, 200, ts(2026, 3, 10, 12, 0))    # week 2 total 1,100 > 1,000
    check('PB pays on beating last week', 'pb' in award_codes(r), str(award_codes(r)))
    check('rolling target pays too', 'weekly_target' in award_codes(r), str(award_codes(r)))

    st, r = run(st, 200, ts(2026, 3, 11, 12, 0))
    check('PB pays only once per week', 'pb' not in award_codes(r), str(award_codes(r)))
    check('target pays only once per week', 'weekly_target' not in award_codes(r),
          str(award_codes(r)))

    # Week 3: more than 1.5x week 2's 1,300 hands earns the Comeback.
    st, r = run(st, 2100, ts(2026, 3, 16, 12, 0))
    check('a >50% volume jump earns 10-2 Doyle Brunson', 'T2' in codes(r), str(codes(r)))


def test_no_comeback_on_a_small_rise():
    st, _ = run({}, 1000, ts(2026, 3, 2, 12, 0))
    st, r = run(st, 1100, ts(2026, 3, 9, 12, 0))    # only +10%
    check('a 10% rise is not a comeback', 'T2' not in codes(r), str(codes(r)))


def test_week_history_window():
    st = {}
    for week_start in (2, 9, 16, 23):
        st, _ = run(st, 500, ts(2026, 3, week_start, 12, 0))
    check('history keeps only the last 3 completed weeks',
          len(st['week_hands_history']) == 3, str(st['week_hands_history']))


# ── 6. Purity ────────────────────────────────────────────────────────────────

def test_evaluate_does_not_mutate_input():
    st, _ = run({}, 500, ts(2026, 3, 2, 12, 0))
    before = {'points_total': st['points_total'],
              'badges': len(st['badges']),
              'milestones': list(st['day_milestones']),
              'recent': list(st['recent_imports'])}
    run(st, 500, ts(2026, 3, 2, 13, 0))
    check('input points untouched', st['points_total'] == before['points_total'])
    check('input badges untouched', len(st['badges']) == before['badges'])
    check('input milestones untouched', st['day_milestones'] == before['milestones'])
    check('input window untouched', st['recent_imports'] == before['recent'])


def test_recent_imports_is_bounded():
    st = {}
    start = ts(2026, 3, 2, 0, 0)
    for i in range(260):
        st, _ = run(st, 20, start + i * 300)        # 5 minutes apart, spans ~21h
    check('rolling window stays capped',
          len(st['recent_imports']) <= G.MAX_RECENT_IMPORTS, str(len(st['recent_imports'])))


# ── 7. Fake Firestore ────────────────────────────────────────────────────────

class _Snap:
    def __init__(self, doc_id, data):
        self.id, self._data, self.exists = doc_id, data, data is not None

    def to_dict(self):
        return dict(self._data or {})


class _Doc:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def get(self, transaction=None):
        return _Snap(self._path[-1], self._store.get(self._path))

    def set(self, data, merge=False):
        cur = dict(self._store.get(self._path) or {}) if merge else {}
        cur.update(data)
        self._store.put(self._path, cur)

    def collection(self, name):
        return _Col(self._store, self._path + (name,))


class _Query:
    def __init__(self, snaps):
        self._snaps = snaps

    def order_by(self, field, direction=None):
        rev = direction == 'DESCENDING'
        return _Query(sorted(self._snaps, key=lambda s: s.to_dict().get(field, 0), reverse=rev))

    def limit(self, n):
        return _Query(self._snaps[:n])

    def get(self):
        return list(self._snaps)


class _Col:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def document(self, doc_id):
        return _Doc(self._store, self._path + (doc_id,))

    def _all(self):
        return [_Snap(k, v) for k, v in self._store.children(self._path)]

    def order_by(self, field, direction=None):
        return _Query(self._all()).order_by(field, direction)

    def limit(self, n):
        return _Query(self._all()).limit(n)

    def get(self):
        return self._all()


class _Txn:
    """Writes land immediately — the tests are single-threaded, so the only behaviour that
    matters here is that reads and writes route through the same store."""

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)


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

    def children(self, path):
        n = len(path)
        return sorted((k[n], v) for k, v in self._d.items()
                      if len(k) == n + 1 and k[:n] == path)


def _patch_firestore():
    """Make @gcf.transactional a pass-through and Query.DESCENDING a plain string.

    gamification.py imports google.cloud.firestore inside its functions, so patching the
    module attributes here is picked up at call time. The real decorator drives a retry
    loop against a live backend and cannot run against FakeDB.
    """
    from google.cloud import firestore as gcf
    gcf.transactional = lambda fn: fn

    class _Q:
        DESCENDING = 'DESCENDING'
    gcf.Query = _Q


# ── 8. Firestore shell ───────────────────────────────────────────────────────

def test_on_import_persists_and_mirrors():
    db = FakeDB()
    when = ts(2026, 3, 2, 12, 0)
    r = G.on_import(db, 'uid-a', 500, claims={'name': 'Alice'}, now_ts=when)
    check('on_import returns a result', r is not None, str(r))
    check('kickoff scored through the shell', r['points'] > 0, str(r))

    state = db.get(('gamification', 'uid-a'))
    check('state document written', state is not None)
    check('uid stamped', state.get('uid') == 'uid-a', str(state.get('uid')))
    check('display name stamped', state.get('display_name') == 'Alice',
          str(state.get('display_name')))
    check('created_at set', state.get('created_at') == when, str(state.get('created_at')))

    entry = db.get(('leaderboards', r['week_key'], 'entries', 'uid-a'))
    check('leaderboard entry mirrored', entry is not None)
    check('entry carries week points', entry.get('points') == r['week_points'], str(entry))
    check('entry carries the safe display name', entry.get('name') == 'Alice', str(entry))


def test_on_import_below_floor_writes_nothing():
    db = FakeDB()
    r = G.on_import(db, 'uid-a', 10, now_ts=ts(2026, 3, 2, 12, 0))
    check('sub-floor import returns nothing', r is None, str(r))
    check('sub-floor import writes no state',
          db.get(('gamification', 'uid-a')) is None, str(db.get(('gamification', 'uid-a'))))


def test_display_name_never_leaks_email():
    name = G.display_name({'email': 'someone@example.com'}, 'abcdef123')
    check('email is not used as a leaderboard name', '@' not in name, name)
    check('falls back to a uid-derived handle', name == 'Player-abcd', name)
    check('auth display name wins', G.display_name({'name': 'Bob'}, 'x') == 'Bob')


def test_settle_week_pays_the_podium():
    db = FakeDB()
    week = '2026-W10'
    for uid, points in (('u1', 100), ('u2', 900), ('u3', 500), ('u4', 300)):
        db.put(('leaderboards', week, 'entries', uid),
               {'uid': uid, 'name': uid, 'points': points, 'hands': points})
        db.put(('gamification', uid), dict(G.blank_state(), uid=uid, points_total=points))

    out = G.settle_week(db, week, now_ts=ts(2026, 3, 9, 1, 0))
    check('settle returns a podium', out and len(out['podium']) == 3, str(out))
    order = [p['uid'] for p in out['podium']]
    check('podium is ordered by points', order == ['u2', 'u3', 'u4'], str(order))
    check('entrant count recorded', out['entrant_count'] == 4, str(out['entrant_count']))

    u2 = db.get(('gamification', 'u2'))
    check('winner paid 2000', u2['points_total'] == 900 + 2000, str(u2['points_total']))
    check('winner got the Straight Flush', 'SF' in u2['badge_codes'], str(u2['badge_codes']))
    check('winner week points untouched', u2['week_points'] == 0, str(u2['week_points']))

    u3 = db.get(('gamification', 'u3'))
    check('runner-up paid 1000', u3['points_total'] == 500 + 1000, str(u3['points_total']))
    check('runner-up got Four of a Kind', 'QUADS' in u3['badge_codes'], str(u3['badge_codes']))

    u4 = db.get(('gamification', 'u4'))
    check('third paid 500', u4['points_total'] == 300 + 500, str(u4['points_total']))
    check('third got the Full House', 'BOAT' in u4['badge_codes'], str(u4['badge_codes']))

    u1 = db.get(('gamification', 'u1'))
    check('fourth place paid nothing', u1['points_total'] == 100, str(u1['points_total']))

    # Settling again must not double-pay.
    again = G.settle_week(db, week, now_ts=ts(2026, 3, 9, 2, 0))
    check('a second settle is a no-op', again is None, str(again))
    check('winner not paid twice', db.get(('gamification', 'u2'))['points_total'] == 2900,
          str(db.get(('gamification', 'u2'))['points_total']))


def test_settle_ignores_an_empty_board():
    db = FakeDB()
    out = G.settle_week(db, '2026-W11', now_ts=ts(2026, 3, 16, 1, 0))
    check('an empty week settles with no podium', out and out['podium'] == [], str(out))
    board = db.get(('leaderboards', '2026-W11'))
    check('the empty week is still marked settled', board.get('settled') is True, str(board))


def test_week_status_flags_pending():
    db = FakeDB()
    now = ts(2026, 3, 16, 12, 0)
    status = G.week_status(db, now_ts=now, tz=ADL, lookback=2)
    check('unsettled weeks are reported pending', len(status['pending']) == 2,
          str(status['pending']))
    check('current week reported', status['current_week'] == '2026-W12',
          str(status['current_week']))

    G.settle_week(db, status['weeks'][0]['week_key'], now_ts=now)
    status = G.week_status(db, now_ts=now, tz=ADL, lookback=2)
    check('settling clears one pending week', len(status['pending']) == 1,
          str(status['pending']))


def test_snapshot():
    db = FakeDB()
    when = ts(2026, 3, 2, 12, 0)
    G.on_import(db, 'uid-a', 500, claims={'name': 'Alice'}, now_ts=when)
    G.on_import(db, 'uid-b', 900, claims={'name': 'Bob'}, now_ts=when)

    snap = G.snapshot(db, 'uid-a', now_ts=when, tz=ADL)
    check('snapshot reports points', snap['points_total'] > 0, str(snap['points_total']))
    check('snapshot reports the live streak', snap['streak_days'] == 1,
          str(snap['streak_days']))
    check('snapshot reports a rank', snap['rank'] in (1, 2), str(snap['rank']))
    check('snapshot names the next volume badge',
          snap['next_badge'] and snap['next_badge']['code'] == '72o', str(snap['next_badge']))

    # Bob out-scored Alice, so he must rank ahead of her.
    check('higher score ranks first', G.snapshot(db, 'uid-b', now_ts=when, tz=ADL)['rank'] == 1,
          str(G.snapshot(db, 'uid-b', now_ts=when, tz=ADL)['rank']))

    # A week later the streak has lapsed and this week's score is zero again.
    later = G.snapshot(db, 'uid-a', now_ts=ts(2026, 3, 12, 12, 0), tz=ADL)
    check('a lapsed streak reports zero', later['streak_days'] == 0, str(later['streak_days']))
    check('last week points do not leak into this week', later['week_points'] == 0,
          str(later['week_points']))

    check('unknown player gets an empty snapshot',
          G.snapshot(db, 'nobody', now_ts=when, tz=ADL)['points_total'] == 0)


def test_badge_catalog():
    catalog = G.badge_catalog(['72o', 'K9'], [
        {'code': '72o', 'name': '7-2 Offsuit', 'title': 'The Grind Begins', 'ts': 100},
        {'code': 'K9', 'name': 'K-9', 'title': 'Break Master', 'ts': 200},
    ])
    check('catalog lists every registered badge', len(catalog) == len(G.BADGES), str(len(catalog)))
    by_code = {b['code']: b for b in catalog}
    check('earned badge is flagged earned', by_code['72o']['earned'] is True)
    check('earned badge carries its unlock ts', by_code['72o']['ts'] == 100, str(by_code['72o']['ts']))
    check('unearned badge is flagged locked', by_code['JJ']['earned'] is False)
    check('unearned badge has no ts', by_code['JJ']['ts'] is None, str(by_code['JJ']['ts']))
    check('every entry carries an icon and a hint',
          all(b['icon'] and b['hint'] for b in catalog))
    check('volume badge categorised as volume', by_code['72o']['category'] == 'volume',
          by_code['72o']['category'])
    check('behavioural badge categorised as behavioural', by_code['K9']['category'] == 'behavioural',
          by_code['K9']['category'])
    check('podium badge categorised as podium', by_code['SF']['category'] == 'podium',
          by_code['SF']['category'])

    empty = G.badge_catalog([], [])
    check('empty state still lists the full registry', len(empty) == len(G.BADGES), str(len(empty)))
    check('nothing is earned in an empty state', not any(b['earned'] for b in empty))


def test_timezone_config_is_read_and_validated():
    db = FakeDB()
    G.invalidate_tz_cache()
    check('default zone when unconfigured', G.resolve_tz_name(db) == 'Australia/Adelaide',
          G.resolve_tz_name(db))

    db.put(('config', 'gamification'), {'timezone': 'UTC'})
    G.invalidate_tz_cache()
    check('configured zone is honoured', G.resolve_tz_name(db) == 'UTC', G.resolve_tz_name(db))

    db.put(('config', 'gamification'), {'timezone': 'Mars/Olympus'})
    G.invalidate_tz_cache()
    check('an unknown zone falls back to the default',
          G.resolve_tz_name(db) == 'Australia/Adelaide', G.resolve_tz_name(db))
    G.invalidate_tz_cache()


# ── 9. HTTP endpoints ────────────────────────────────────────────────────────

def test_http_endpoints():
    import json
    import app as A

    db = FakeDB()
    A._get_admin_db     = lambda: db
    A._get_admin_bucket = lambda: object()      # a configured bucket

    caller = {'uid': 'uid-a', 'admin': True}
    A._verify_bearer = lambda req: caller['uid']
    A._is_admin      = lambda uid: caller['admin']

    client = A.app.test_client()

    def js(res):
        return res.status_code, json.loads(res.get_data(as_text=True))

    # A player with no history still gets a well-formed banner payload.
    status, body = js(client.get('/api/gamification'))
    check('gamification endpoint returns 200', status == 200, str(status))
    check('empty player reports zero points', body['points_total'] == 0, str(body))
    check('empty player is told the minimum', body['min_hands'] == G.MIN_HANDS, str(body))

    caller['uid'] = None
    status, _ = js(client.get('/api/gamification'))
    check('signed-out players are rejected', status == 401, str(status))
    caller['uid'] = 'uid-a'

    # Score an import through the app-level helper, then read it back.
    A._score_import({'uid': 'uid-a', 'name': 'Alice'}, 600)
    status, body = js(client.get('/api/gamification'))
    check('scored points surface on the endpoint', body['points_total'] > 0, str(body))

    # ── the two guards on _score_import ──
    check('no claims means no scoring', A._score_import(None, 600) is None)
    check('zero new hands means no scoring',
          A._score_import({'uid': 'uid-b'}, 0) is None)

    A._get_admin_bucket = lambda: None
    print('--- expected warning below: the unconfigured-bucket guard is under test ---')
    check('an unconfigured bucket disables scoring',
          A._score_import({'uid': 'uid-b'}, 600) is None)
    print('--- end expected warning ---')
    check('and writes no state for that player',
          db.get(('gamification', 'uid-b')) is None)
    A._get_admin_bucket = lambda: object()

    # ── admin surface ──
    caller['admin'] = False
    status, _ = js(client.get('/api/admin/gamification'))
    check('non-admins cannot read settings', status == 403, str(status))
    status, _ = js(client.post('/api/admin/gamification',
                               data=json.dumps({'timezone': 'UTC'}),
                               content_type='application/json'))
    check('non-admins cannot change the timezone', status == 403, str(status))
    caller['admin'] = True

    status, body = js(client.get('/api/admin/gamification'))
    check('admin settings load', status == 200, str(status))
    check('settings offer the timezone list', 'Australia/Adelaide' in body['timezones'],
          str(body.get('timezones')))
    check('settings report pending weeks', isinstance(body['pending'], list), str(body))

    status, body = js(client.post('/api/admin/gamification',
                                  data=json.dumps({'timezone': 'Mars/Olympus'}),
                                  content_type='application/json'))
    check('an unknown timezone is rejected', status == 400, str(status))
    check('the rejection names the zone', 'Mars/Olympus' in body.get('error', ''), str(body))
    check('the rejected zone was not written',
          db.get(('config', 'gamification')) is None, str(db.get(('config', 'gamification'))))

    status, _ = js(client.post('/api/admin/gamification',
                               data=json.dumps({'timezone': 'UTC'}),
                               content_type='application/json'))
    check('a known timezone is accepted', status == 200, str(status))
    check('the timezone is persisted',
          (db.get(('config', 'gamification')) or {}).get('timezone') == 'UTC',
          str(db.get(('config', 'gamification'))))
    G.invalidate_tz_cache()

    # ── settlement ──
    status, _ = js(client.post('/api/admin/gamification/settle',
                               data=json.dumps({}), content_type='application/json'))
    check('settling needs a week key', status == 400, str(status))

    current = G.week_key(int(__import__('time').time()), G.resolve_tz(db))
    status, body = js(client.post('/api/admin/gamification/settle',
                                  data=json.dumps({'week_key': current}),
                                  content_type='application/json'))
    check('the open week cannot be settled', status == 400, str(status))
    check('and the refusal says why', 'still open' in body.get('error', ''), str(body))

    status, body = js(client.post('/api/admin/gamification/settle',
                                  data=json.dumps({'week_key': '2026-W02'}),
                                  content_type='application/json'))
    check('a closed week settles', status == 200, str(status))
    status, body = js(client.post('/api/admin/gamification/settle',
                                  data=json.dumps({'week_key': '2026-W02'}),
                                  content_type='application/json'))
    check('settling twice is reported as already settled',
          body.get('already_settled') is True, str(body))

    caller['admin'] = False
    status, _ = js(client.post('/api/admin/gamification/settle',
                               data=json.dumps({'week_key': '2026-W03'}),
                               content_type='application/json'))
    check('non-admins cannot settle', status == 403, str(status))

    G.invalidate_tz_cache()


def main():
    _patch_firestore()

    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                problems.append('%s raised %s: %s' % (name, type(exc).__name__, exc))

    for p in problems:
        print('  FAIL', p)
    print('gamification: ' + ('PASS' if not problems else 'FAIL'))
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
