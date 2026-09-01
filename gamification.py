"""
gamification.py — points, streaks, badges and weekly leaderboards for hand imports.

The module is deliberately split in two halves:

  * a **pure core** — `evaluate(state, new_hands, now_ts, tz)` — that takes the player's
    stored state and one import event and returns the new state plus a description of what
    was earned. It touches no clock, no network and no Firestore, so all ~30 interacting
    rules can be tested as plain data in / data out (see test_gamification.py).
  * a **Firestore shell** — `on_import`, `settle_week`, `snapshot` — that reads and writes
    documents around that core inside transactions.

The day/week boundaries, the 2-5am Steel Wheel and the 23:55 Closer are evaluated in the zone
configured at `config/gamification.timezone`, defaulting to Australia/Adelaide. UTC day
boundaries would reset the daily streak at ~09:30 Adelaide time — mid-morning, halfway through
a player's day — which is why those follow the player's clock rather than the server's.

The :55 break window is the exception: it tracks the real tournament break, which is aligned to
the top of the UTC hour, so it is measured against UTC minute-of-hour rather than the configured
zone. A +X:30 zone like Adelaide is offset by 30 minutes, so scoring its break window in local
time would credit it at :55 local (UTC :25) — thirty minutes off the actual :55-UTC break.

State lives in the top-level `gamification/{uid}` collection, NOT under `users/{uid}`.
firestore.rules lets a user write any non-paid field on their own user document and anything
at all in its subcollections, so points stored there could be edited from the browser
console. The top-level collection is Admin-SDK-write-only.
"""

import time
import traceback
from datetime import date, datetime
from zoneinfo import ZoneInfo

# ── Economy constants ────────────────────────────────────────────────────────

# An import must bring at least this many genuinely-new hands to score anything at all.
# Below the threshold nothing happens: no points, no badges, no streak, and the import is
# not even recorded in the rolling window (so a burst of tiny imports cannot fake a surge).
MIN_HANDS = 15

POINTS_KICKOFF      = 50    # first valid import of the day
POINTS_BASE         = 10    # every later import that day
POINTS_BREAK_SYNC   = 25    # imported between :55 and the top of the hour
POINTS_SURGE        = 40    # 4th import inside a rolling 60 minutes
POINTS_STREAK_STEP  = 10    # per consecutive day, ...
POINTS_STREAK_CAP   = 100   # ... capped here
POINTS_PB           = 500   # beat last week's hand volume
POINTS_TARGET       = 300   # hit the rolling 3-week average

# Daily cumulative-hand bands. Each floor pays once per day, and they stack: a 900-hand day
# crosses 30, 200 and 750 and so earns 20 + 50 + 150.
MILESTONE_BANDS = ((30, 20), (200, 50), (750, 150), (2000, 400))
MILESTONE_5K_STEP   = 5000
MILESTONE_5K_POINTS = 1000

SURGE_WINDOW_SECS   = 3600      # rolling window for the Hourly Surge / The Wheel
SURGE_THRESHOLD     = 4
MACHINE_WINDOW_SECS = 86400     # rolling window for The Machine
MACHINE_THRESHOLD   = 10
BREAK_MINUTE        = 55        # :55 → :00 is the "break time" window
BREAK_STREAK_TARGET = 5         # consecutive break-window days for K-9
STEEL_WHEEL_HOURS   = (2, 5)    # [2am, 5am)
ROYAL_STREAK        = 7
DOWNSWING_STREAK    = 10        # a streak this long or longer, when broken, earns 2-3o
GHOST_GAP_DAYS      = 8         # 8 days between imports == exactly 7 full silent days
WEEK_HISTORY_LEN    = 3         # rolling average window for the weekly target
MAX_RECENT_IMPORTS  = 200       # hard cap so the state document stays small

DEFAULT_TZ = 'Australia/Adelaide'

# Mirrors the timezone picker in templates/index.html so the admin cannot configure a zone
# the rest of the app has no concept of.
ALLOWED_TIMEZONES = (
    'Australia/Adelaide', 'Australia/Brisbane', 'Australia/Darwin',
    'Australia/Melbourne', 'Australia/Perth', 'Australia/Sydney', 'UTC',
)

# ── Badge registry ───────────────────────────────────────────────────────────
# `name` is the poker hand, `title` the nickname players actually use for it.

BADGES = {
    # Lifetime volume
    '72o':   {'name': '7-2 Offsuit',     'title': 'The Grind Begins',  'icon': '🌱', 'hint': 'Import 1,000 hands, lifetime.'},
    '44':    {'name': 'Pocket Fours',    'title': 'The Sailboats',     'icon': '⛵', 'hint': 'Import 10,000 hands, lifetime.'},
    'JJ':    {'name': 'Pocket Jacks',    'title': 'Mid-Tier Reg',      'icon': '🎯', 'hint': 'Import 25,000 hands, lifetime.'},
    'QQ':    {'name': 'Pocket Queens',   'title': 'The Ladies',        'icon': '💃', 'hint': 'Import 50,000 hands, lifetime.'},
    'KK':    {'name': 'Pocket Kings',    'title': 'Volume Crusher',    'icon': '💪', 'hint': 'Import 100,000 hands, lifetime.'},
    'AK':    {'name': 'A-K',             'title': 'Big Slick',         'icon': '🎩', 'hint': 'Import 250,000 hands, lifetime.'},
    'AA':    {'name': 'Pocket Aces',     'title': 'The Legend',        'icon': '🏆', 'hint': 'Import 500,000 hands, lifetime.'},
    'AA88':  {'name': 'A-A-8-8',         'title': "Dead Man's Hand",   'icon': '💀', 'hint': 'Import 1,000,000 hands, lifetime.'},
    # Behavioural
    'K9':    {'name': 'K-9',             'title': 'Break Master',      'icon': '☕', 'hint': 'Import during the tournament break, 5 days running.'},
    'A2345': {'name': 'A-2-3-4-5',       'title': 'Rapid Fire',        'icon': '⚡', 'hint': 'Import 4 times inside one rolling hour.'},
    'STEEL': {'name': 'Steel Wheel',     'title': 'Vampire Grinder',   'icon': '🧛', 'hint': 'Import between 2am and 5am.'},
    'T2':    {'name': '10-2',            'title': 'The Comeback',      'icon': '🔄', 'hint': 'Import 50% more hands than last week.'},
    'ROYAL': {'name': 'Royal Flush',     'title': 'Flawless Week',     'icon': '🌟', 'hint': 'Keep a 7-day import streak alive.'},
    'Q7':    {'name': 'Q-7',             'title': 'The Machine',       'icon': '🤖', 'hint': 'Import 10 times inside 24 hours.'},
    'J4':    {'name': 'J-4',             'title': 'The Wildcard',      'icon': '🃏', 'hint': 'Import exactly 15 hands in one batch.'},
    '99':    {'name': '9-9',             'title': 'The Closer',        'icon': '🔒', 'hint': 'Import in the last 5 minutes before the daily reset.'},
    '83':    {'name': '8-3',             'title': 'The Ghost',         'icon': '👻', 'hint': 'Return after exactly 7 silent days.'},
    '23o':   {'name': '2-3 Offsuit',     'title': 'The Downswing',     'icon': '📉', 'hint': 'Break a 10-day-or-longer streak.'},
    # Weekly podium
    'SF':    {'name': 'Straight Flush',  'title': 'Weekly Champion',   'icon': '🥇', 'hint': 'Finish #1 on the weekly leaderboard.'},
    'QUADS': {'name': 'Four of a Kind',  'title': 'Weekly Runner-Up',  'icon': '🥈', 'hint': 'Finish #2 on the weekly leaderboard.'},
    'BOAT':  {'name': 'Full House',      'title': 'Weekly Third',      'icon': '🥉', 'hint': 'Finish #3 on the weekly leaderboard.'},
}

VOLUME_BADGES = (
    (1000, '72o'), (10000, '44'), (25000, 'JJ'), (50000, 'QQ'),
    (100000, 'KK'), (250000, 'AK'), (500000, 'AA'), (1000000, 'AA88'),
)

# rank → (points, badge code)
PODIUM = {1: (2000, 'SF'), 2: (1000, 'QUADS'), 3: (500, 'BOAT')}


# ── Time helpers ─────────────────────────────────────────────────────────────

def local_dt(ts, tz):
    return datetime.fromtimestamp(ts, tz)


def utc_minute(ts):
    """Minute-of-hour on the UTC clock, independent of the configured display zone.

    The tournament break (:55 -> :00) is a fixed real-world event aligned to the top of the
    UTC hour. Whole-hour zones share UTC's minute-of-hour, but +X:30 zones (Adelaide, Darwin)
    are offset by 30, so the break window must be measured against UTC -- otherwise Adelaide's
    break lands at :25 local instead of :55.
    """
    return int(ts // 60) % 60


def day_key(ts, tz):
    """'YYYY-MM-DD' in the configured zone — the daily leaderboard bucket."""
    return local_dt(ts, tz).strftime('%Y-%m-%d')


def week_key(ts, tz):
    """'YYYY-Www' ISO week in the configured zone. Weeks start Monday 00:00 local."""
    iso = local_dt(ts, tz).isocalendar()
    return '%04d-W%02d' % (iso[0], iso[1])


def prev_week_key(ts, tz):
    """The ISO week before the one containing `ts`. Seven days back always lands there."""
    return week_key(ts - 604800, tz)


def _day_gap(from_key, to_key):
    """Whole days between two 'YYYY-MM-DD' keys, or None if either is missing/malformed."""
    if not from_key or not to_key:
        return None
    try:
        return (date.fromisoformat(to_key) - date.fromisoformat(from_key)).days
    except ValueError:
        return None


# ── State ────────────────────────────────────────────────────────────────────

def blank_state():
    """The shape every gamification document has. New keys must be added here too, because
    `evaluate` layers the stored document over this and relies on every key existing."""
    return {
        'points_total':        0,
        'lifetime_hands':      0,

        'day_key':             '',
        'day_hands':           0,
        'day_imports':         0,
        'day_milestones':      [],   # band floors already paid today
        'day_5k_paid':         0,    # 5k increments already paid today
        'surge_armed':         True,

        'streak_days':         0,
        'streak_last_day':     '',
        'streak_best':         0,

        'week_key':            '',
        'week_points':         0,
        'week_hands':          0,
        'week_pb_awarded':     False,
        'week_target_awarded': False,
        'prev_week_hands':     0,
        'week_hands_history':  [],   # last 3 *completed* weeks, newest first

        'break_streak_days':   0,
        'break_last_day':      '',

        'recent_imports':      [],   # epoch seconds, trailing 24h, newest last
        'last_import_ts':      0,
        'last_import_day':     '',

        'badges':              [],   # [{code, name, title, ts}]
        'badge_codes':         [],
    }


def _hydrate(state):
    """Stored document → a complete, deeply-copied working state.

    The copy matters: `evaluate` is documented as pure, and callers (notably the retryable
    Firestore transaction) may run it more than once against the same input dict.
    """
    st = blank_state()
    st.update(state or {})
    st['day_milestones']     = list(st.get('day_milestones') or [])
    st['week_hands_history'] = list(st.get('week_hands_history') or [])
    st['recent_imports']     = list(st.get('recent_imports') or [])
    st['badge_codes']        = list(st.get('badge_codes') or [])
    st['badges']             = [dict(b) for b in (st.get('badges') or [])]
    return st


def _rolling_target(history):
    """Personalised weekly hand target: the mean of the last completed weeks."""
    if not history:
        return 0
    return int(sum(history) / len(history))


# ── The pure scoring core ────────────────────────────────────────────────────

def evaluate(state, new_hands, now_ts, tz):
    """Score one import. Returns (new_state, result); never mutates `state`.

    `result` is what the client is told: the points breakdown, any badges unlocked, and the
    running totals the banner shows.
    """
    st = _hydrate(state)

    if new_hands < MIN_HANDS:
        # Anti-spam floor. Returning the hydrated (unchanged) state keeps the caller's
        # write path uniform, but `skipped` tells it there is nothing worth persisting.
        return st, {
            'skipped':        'min_hands',
            'min_hands':      MIN_HANDS,
            'new_hands':      new_hands,
            'points':         0,
            'awards':         [],
            'badges':         [],
            'points_total':   st['points_total'],
            'week_points':    st['week_points'],
            'streak_days':    st['streak_days'],
            'lifetime_hands': st['lifetime_hands'],
            'day_hands':      st['day_hands'],
            'week_hands':     st['week_hands'],
        }

    local     = local_dt(now_ts, tz)
    today     = day_key(now_ts, tz)
    this_week = week_key(now_ts, tz)

    awards, unlocked = [], []

    def add(code, label, points):
        if points:
            awards.append({'code': code, 'label': label, 'points': points})

    def unlock(code):
        if code in st['badge_codes']:
            return
        meta  = BADGES[code]
        entry = {'code': code, 'name': meta['name'], 'title': meta['title'], 'ts': now_ts}
        st['badge_codes'].append(code)
        st['badges'].append(entry)
        unlocked.append(entry)

    # ── Day rollover ─────────────────────────────────────────────────────────
    # Read the previous import day before anything overwrites it: both the streak and the
    # Ghost badge are measured from it.
    prev_day = st['last_import_day']

    if st['day_key'] != today:
        gap = _day_gap(prev_day, today)
        if gap == 1:
            st['streak_days'] += 1
        else:
            # A streak of 10+ days is worth mourning, so the break itself is a badge.
            if st['streak_days'] >= DOWNSWING_STREAK:
                unlock('23o')
            st['streak_days'] = 1
        if gap == GHOST_GAP_DAYS:
            unlock('83')

        st['streak_best']    = max(st['streak_best'], st['streak_days'])
        st['day_key']        = today
        st['day_hands']      = 0
        st['day_imports']    = 0
        st['day_milestones'] = []
        st['day_5k_paid']    = 0

    first_of_day = st['day_imports'] == 0

    # ── Week rollover ────────────────────────────────────────────────────────
    if st['week_key'] != this_week:
        if st['week_key']:  # not the player's very first week
            st['prev_week_hands']    = st['week_hands']
            st['week_hands_history'] = ([st['week_hands']] +
                                        st['week_hands_history'])[:WEEK_HISTORY_LEN]
        st['week_key']            = this_week
        st['week_points']         = 0
        st['week_hands']          = 0
        st['week_pb_awarded']     = False
        st['week_target_awarded'] = False

    # ── Rolling import windows ───────────────────────────────────────────────
    # Prune to 24h, then record this import. Both the 60-minute surge and the 24-hour
    # Machine badge read from this one list.
    recent = [t for t in st['recent_imports'] if t > now_ts - MACHINE_WINDOW_SECS]
    recent.append(now_ts)
    st['recent_imports'] = recent[-MAX_RECENT_IMPORTS:]
    in_hour = sum(1 for t in st['recent_imports'] if t > now_ts - SURGE_WINDOW_SECS)
    in_day  = len(st['recent_imports'])

    # ── Points ───────────────────────────────────────────────────────────────
    if first_of_day:
        add('kickoff', 'Daily Kickoff', POINTS_KICKOFF)
        add('streak', 'Day %d Streak' % st['streak_days'],
            min(POINTS_STREAK_STEP * st['streak_days'], POINTS_STREAK_CAP))
    else:
        add('base', 'Import', POINTS_BASE)

    # The break follows the tournament clock (top of the UTC hour), NOT the configured display
    # zone — otherwise a +X:30 zone like Adelaide would credit the break at :55 local (UTC :25),
    # thirty minutes off the real :55-UTC break.
    in_break = utc_minute(now_ts) >= BREAK_MINUTE
    if in_break:
        add('break_sync', 'Break-Time Sync', POINTS_BREAK_SYNC)

    # The surge fires on the 4th import in the window and then disarms, so imports 5, 6, 7…
    # inside the same busy hour do not each pay again. It re-arms once the window drains.
    if in_hour >= SURGE_THRESHOLD:
        if st['surge_armed']:
            add('surge', 'Hourly Surge', POINTS_SURGE)
            unlock('A2345')
            st['surge_armed'] = False
    else:
        st['surge_armed'] = True

    st['day_hands']      += new_hands
    st['week_hands']     += new_hands
    st['lifetime_hands'] += new_hands

    for floor, points in MILESTONE_BANDS:
        if floor not in st['day_milestones'] and st['day_hands'] >= floor:
            st['day_milestones'].append(floor)
            add('milestone_%d' % floor, '%s hands today' % format(floor, ','), points)

    increments = st['day_hands'] // MILESTONE_5K_STEP
    if increments > st['day_5k_paid']:
        unpaid = increments - st['day_5k_paid']
        st['day_5k_paid'] = increments
        add('milestone_5k', '%s hands today' % format(increments * MILESTONE_5K_STEP, ','),
            unpaid * MILESTONE_5K_POINTS)

    if (not st['week_pb_awarded'] and st['prev_week_hands'] > 0
            and st['week_hands'] > st['prev_week_hands']):
        st['week_pb_awarded'] = True
        add('pb', 'Personal Best Week', POINTS_PB)

    target = _rolling_target(st['week_hands_history'])
    if not st['week_target_awarded'] and target > 0 and st['week_hands'] >= target:
        st['week_target_awarded'] = True
        add('weekly_target', 'Weekly Target', POINTS_TARGET)

    # ── Badges ───────────────────────────────────────────────────────────────
    for threshold, code in VOLUME_BADGES:
        if st['lifetime_hands'] >= threshold:
            unlock(code)

    if new_hands == MIN_HANDS:
        unlock('J4')                                    # exactly 15 hands in one batch

    if in_break:
        if st['break_last_day'] != today:
            gap = _day_gap(st['break_last_day'], today)
            st['break_streak_days'] = (st['break_streak_days'] + 1) if gap == 1 else 1
            st['break_last_day']    = today
        if st['break_streak_days'] >= BREAK_STREAK_TARGET:
            unlock('K9')

    if STEEL_WHEEL_HOURS[0] <= local.hour < STEEL_WHEEL_HOURS[1]:
        unlock('STEEL')

    # The last five minutes before the daily reset.
    if local.hour == 23 and local.minute >= BREAK_MINUTE:
        unlock('99')

    if in_day >= MACHINE_THRESHOLD:
        unlock('Q7')

    if st['streak_days'] >= ROYAL_STREAK:
        unlock('ROYAL')

    if st['prev_week_hands'] > 0 and st['week_hands'] > 1.5 * st['prev_week_hands']:
        unlock('T2')

    # ── Commit ───────────────────────────────────────────────────────────────
    earned = sum(a['points'] for a in awards)
    st['points_total']    += earned
    st['week_points']     += earned
    st['day_imports']     += 1
    st['last_import_ts']   = now_ts
    st['last_import_day']  = today
    st['streak_last_day']  = today

    return st, {
        'skipped':        None,
        'new_hands':      new_hands,
        'points':         earned,
        'awards':         awards,
        'badges':         unlocked,
        'points_total':   st['points_total'],
        'week_points':    st['week_points'],
        'streak_days':    st['streak_days'],
        'lifetime_hands': st['lifetime_hands'],
        'day_hands':      st['day_hands'],
        'week_hands':     st['week_hands'],
        'week_key':       st['week_key'],
    }


def next_volume_badge(lifetime_hands):
    """The next lifetime milestone and how far away it is, for the banner's progress hint."""
    for threshold, code in VOLUME_BADGES:
        if lifetime_hands < threshold:
            meta = BADGES[code]
            return {'code': code, 'name': meta['name'], 'title': meta['title'],
                    'threshold': threshold, 'remaining': threshold - lifetime_hands}
    return None


_VOLUME_CODES = frozenset(code for _, code in VOLUME_BADGES)
_PODIUM_CODES = frozenset(code for _, code in PODIUM.values())


def badge_catalog(badge_codes, badges_earned):
    """Every badge in the registry, earned or not — feeds the header's "badge journey" popup
    so players can see what they're chasing, not just what they already have."""
    earned_ts = {b['code']: b['ts'] for b in badges_earned}
    catalog = []
    for code, meta in BADGES.items():
        category = ('volume' if code in _VOLUME_CODES else
                    'podium' if code in _PODIUM_CODES else 'behavioural')
        catalog.append({
            'code':     code,
            'name':     meta['name'],
            'title':    meta['title'],
            'icon':     meta['icon'],
            'hint':     meta['hint'],
            'category': category,
            'earned':   code in badge_codes,
            'ts':       earned_ts.get(code),
        })
    return catalog


# ── Firestore shell ──────────────────────────────────────────────────────────

_TZ_CACHE = {'ts': 0, 'name': None}
_TZ_TTL   = 60   # seconds; a stale cache also self-heals when gunicorn recycles the worker


def resolve_tz_name(db):
    """Configured zone name from config/gamification, falling back to Adelaide."""
    now = int(time.time())
    if _TZ_CACHE['name'] and now - _TZ_CACHE['ts'] < _TZ_TTL:
        return _TZ_CACHE['name']
    name = DEFAULT_TZ
    try:
        snap = db.collection('config').document('gamification').get()
        if snap.exists:
            name = (snap.to_dict() or {}).get('timezone') or DEFAULT_TZ
    except Exception as exc:
        # A config read failure must not stop scoring — but log it, because silently
        # scoring in the wrong zone shifts every day boundary.
        print('[gamification] timezone config read failed: %s: %s' % (type(exc).__name__, exc))
    if name not in ALLOWED_TIMEZONES:
        name = DEFAULT_TZ
    _TZ_CACHE.update(ts=now, name=name)
    return name


def resolve_tz(db):
    return ZoneInfo(resolve_tz_name(db))


def invalidate_tz_cache():
    """Called by the admin save path so a zone change takes effect immediately."""
    _TZ_CACHE.update(ts=0, name=None)


def display_name(claims, uid):
    """Leaderboard-safe name.

    Leaderboard entries are readable by every signed-in user, so the email address is never
    used: a player with no Auth display name shows up as Player-<uid prefix> instead.
    """
    name = ((claims or {}).get('name') or '').strip()
    return name or ('Player-%s' % (uid or '')[:4])


def _entry_ref(db, week, uid):
    return (db.collection('leaderboards').document(week)
              .collection('entries').document(uid))


def on_import(db, uid, new_hands, claims=None, now_ts=None):
    """Score one import and mirror the result onto the weekly leaderboard.

    Returns the result payload, or None when nothing was scored. Never raises: a bug in the
    economy must not turn a successful hand import into a failed request.
    """
    try:
        now_ts = now_ts or int(time.time())
        tz     = resolve_tz(db)
        result = _apply_import(db, uid, new_hands, claims, now_ts, tz)
    except Exception as exc:
        print('[gamification] scoring failed for uid=%s: %s: %s'
              % (uid, type(exc).__name__, exc))
        traceback.print_exc()
        return None

    try:
        maybe_settle(db, now_ts, tz)
    except Exception as exc:
        # Settlement is best-effort here; the admin panel exposes a manual Settle button
        # precisely so a failure at this point is recoverable.
        print('[gamification] weekly settle failed: %s: %s' % (type(exc).__name__, exc))

    return None if result.get('skipped') else result


def _apply_import(db, uid, new_hands, claims, now_ts, tz):
    from google.cloud import firestore as gcf

    doc_ref = db.collection('gamification').document(uid)
    name    = display_name(claims, uid)

    @gcf.transactional
    def _txn(transaction):
        snap  = doc_ref.get(transaction=transaction)
        state = snap.to_dict() if snap.exists else {}
        new_state, result = evaluate(state, new_hands, now_ts, tz)
        if result.get('skipped'):
            return result

        new_state['uid']          = uid
        new_state['display_name'] = name
        new_state['created_at']   = (state or {}).get('created_at') or now_ts
        new_state['updated_at']   = now_ts
        transaction.set(doc_ref, new_state)

        transaction.set(_entry_ref(db, new_state['week_key'], uid), {
            'uid':        uid,
            'name':       name,
            'points':     new_state['week_points'],
            'hands':      new_state['week_hands'],
            'updated_at': now_ts,
        })
        return result

    return _txn(db.transaction())


# ── Weekly settlement ────────────────────────────────────────────────────────

def maybe_settle(db, now_ts=None, tz=None):
    """Settle last week if it closed without being settled. Cheap no-op once done."""
    now_ts = now_ts or int(time.time())
    tz     = tz or resolve_tz(db)
    week   = prev_week_key(now_ts, tz)
    snap   = db.collection('leaderboards').document(week).get()
    if snap.exists and (snap.to_dict() or {}).get('settled'):
        return None
    return settle_week(db, week, now_ts)


def settle_week(db, week, now_ts=None):
    """Pay the podium for a closed week. Idempotent — a second call is a no-op.

    Podium points land on `points_total` only, never on the winner's current `week_points`:
    the prize is for last week, and adding it to this week's score would hand the leader a
    head start on the next leaderboard.
    """
    from google.cloud import firestore as gcf

    now_ts    = now_ts or int(time.time())
    board_ref = db.collection('leaderboards').document(week)

    # Read the standings before claiming. The week is closed, so nothing can change them,
    # and computing first means the claim transaction stays a single tiny write.
    # Reading every entry (rather than limit(3)) also gives the admin panel its entrant
    # count for free; at this roster size that is one small query.
    entries = []
    for snap in board_ref.collection('entries').get():
        d = snap.to_dict() or {}
        entries.append({'uid': d.get('uid') or snap.id,
                        'name': d.get('name') or '',
                        'points': int(d.get('points') or 0),
                        'hands': int(d.get('hands') or 0)})
    entries.sort(key=lambda e: e['points'], reverse=True)

    podium = []
    for rank, entry in enumerate(entries[:3], start=1):
        if entry['points'] <= 0:
            break   # an empty scoreboard has no winners
        points, badge = PODIUM[rank]
        podium.append({'rank': rank, 'uid': entry['uid'], 'name': entry['name'],
                       'points': entry['points'], 'awarded': points, 'badge': badge})

    # Claim the week atomically so two concurrent imports cannot both pay out.
    @gcf.transactional
    def _claim(transaction):
        snap = board_ref.get(transaction=transaction)
        if snap.exists and (snap.to_dict() or {}).get('settled'):
            return False
        transaction.set(board_ref, {
            'week_key':      week,
            'settled':       True,
            'settled_at':    now_ts,
            'podium':        podium,
            'entrant_count': len(entries),
        }, merge=True)
        return True

    if not _claim(db.transaction()):
        return None

    for place in podium:
        try:
            _award_podium(db, place['uid'], place['awarded'], place['badge'], now_ts)
        except Exception as exc:
            # One unreachable player must not block the rest of the podium being paid.
            print('[gamification] podium payout failed for uid=%s: %s: %s'
                  % (place['uid'], type(exc).__name__, exc))

    return {'week_key': week, 'podium': podium, 'entrant_count': len(entries),
            'settled_at': now_ts}


def _award_podium(db, uid, points, badge, now_ts):
    from google.cloud import firestore as gcf

    doc_ref = db.collection('gamification').document(uid)

    @gcf.transactional
    def _txn(transaction):
        snap = doc_ref.get(transaction=transaction)
        if not snap.exists:
            return
        st = _hydrate(snap.to_dict())
        st['points_total'] += points
        if badge not in st['badge_codes']:
            meta = BADGES[badge]
            st['badge_codes'].append(badge)
            st['badges'].append({'code': badge, 'name': meta['name'],
                                 'title': meta['title'], 'ts': now_ts})
        st['updated_at'] = now_ts
        transaction.set(doc_ref, st)

    return _txn(db.transaction())


def week_status(db, now_ts=None, tz=None, lookback=8):
    """Recent weeks and whether each has been settled — drives the admin panel and its
    unsettled-week warning flag."""
    now_ts  = now_ts or int(time.time())
    tz      = tz or resolve_tz(db)
    current = week_key(now_ts, tz)

    weeks = []
    for i in range(1, lookback + 1):
        wk   = week_key(now_ts - i * 604800, tz)
        snap = db.collection('leaderboards').document(wk).get()
        data = (snap.to_dict() or {}) if snap.exists else {}
        weeks.append({
            'week_key':      wk,
            'closed':        True,
            'settled':       bool(data.get('settled')),
            'settled_at':    data.get('settled_at'),
            'podium':        data.get('podium') or [],
            'entrant_count': data.get('entrant_count', 0),
        })
    return {'current_week': current, 'timezone': resolve_tz_name(db), 'weeks': weeks,
            'pending': [w['week_key'] for w in weeks if not w['settled']]}


# ── Read model for the banner ────────────────────────────────────────────────

def snapshot(db, uid, now_ts=None, tz=None):
    """Everything the header banner shows for one player."""
    now_ts = now_ts or int(time.time())
    tz     = tz or resolve_tz(db)

    snap = db.collection('gamification').document(uid).get()
    if not snap.exists:
        return {
            'points_total': 0, 'week_points': 0, 'streak_days': 0, 'streak_best': 0,
            'lifetime_hands': 0, 'rank': None, 'badges': [],
            'next_badge': next_volume_badge(0), 'timezone': resolve_tz_name(db),
            'min_hands': MIN_HANDS, 'badge_catalog': badge_catalog([], []),
        }

    st        = _hydrate(snap.to_dict())
    today     = day_key(now_ts, tz)
    this_week = week_key(now_ts, tz)

    # A streak is only live if the last import was today or yesterday; anything older has
    # already lapsed, and showing the stale number would be a lie the next import corrects.
    gap    = _day_gap(st['last_import_day'], today)
    streak = st['streak_days'] if gap in (0, 1) else 0

    week_points = st['week_points'] if st['week_key'] == this_week else 0
    rank = _current_rank(db, uid, this_week) if week_points else None

    return {
        'points_total':   st['points_total'],
        'week_points':    week_points,
        'streak_days':    streak,
        'streak_best':    st['streak_best'],
        'lifetime_hands': st['lifetime_hands'],
        'rank':           rank,
        'badges':         st['badges'],
        'next_badge':     next_volume_badge(st['lifetime_hands']),
        'timezone':       resolve_tz_name(db),
        'min_hands':      MIN_HANDS,
        'badge_catalog':  badge_catalog(st['badge_codes'], st['badges']),
    }


def _current_rank(db, uid, week, cap=200):
    """1-based rank on this week's board, or None if outside the top `cap`.

    Ordering by a single field needs no composite index, and the cap bounds the read.
    """
    from google.cloud import firestore as gcf
    try:
        query = (db.collection('leaderboards').document(week).collection('entries')
                   .order_by('points', direction=gcf.Query.DESCENDING).limit(cap))
        for i, snap in enumerate(query.get(), start=1):
            if snap.id == uid:
                return i
    except Exception as exc:
        print('[gamification] rank lookup failed: %s: %s' % (type(exc).__name__, exc))
    return None
