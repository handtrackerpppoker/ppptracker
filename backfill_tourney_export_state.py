#!/usr/bin/env python3
"""
backfill_tourney_export_state.py — one-shot, disposable.

Writes users/{uid}/quota/tourney_export with {lifetime_free_used: False} for
every user that doesn't already have that subdocument.

Why "everyone starts with lifetime_free_used = False" rather than trying to
infer it from history: the old tourney-export model gated *every* export
behind a survey/credit from day one (FREE_TOURNEY_EXPORTS_DAY == 1, and that
one slot has always been fully survey-gated — see _EXPORT_ADS_DEFAULTS in
app.py). So "has this user exported a tournament before" says nothing about
whether they should get the *new* lifetime freebie — under the old rules
everyone who ever exported already paid for it with a survey. Treating
lifetime_free_used as unearned for existing users costs one extra ungated
export per user, once, and is simpler and more goodwill-generous than trying
to reconstruct an equivalent from _quota_state/quota.tourney_exports history
that was never designed to answer this question.

_tourney_export_state() in app.py already treats a *missing* subdocument as
lifetime_free_used=False, so this script is not strictly required for
correctness — it exists to make the state visible in Firestore immediately
(for anyone inspecting the console or writing a report) rather than only
appearing lazily on first read. Users created after this script runs get
their tourney_export doc lazily via _bump_tourney_export_usage() the first
time it matters; nothing here needs to run again.

Run once against prod, then delete this file — see docs/firestore-schema.md
for the field this populates.

    FIREBASE_SERVICE_ACCOUNT_JSON='...' python backfill_tourney_export_state.py
"""

import os

os.environ.setdefault('FIREBASE_STORAGE_BUCKET', 'unused-by-this-script')

import app as A  # noqa: E402


def main():
    db = A._get_admin_db()

    updated, skipped = 0, 0
    for doc in db.collection('users').stream():
        uid = doc.id
        quota_ref = db.collection('users').document(uid).collection('quota').document('tourney_export')
        if quota_ref.get().exists:
            skipped += 1
            continue

        quota_ref.create({'lifetime_free_used': False})
        print(f'  [set] {uid} -> lifetime_free_used: False')
        updated += 1

    print(f'\nDone. {updated} users backfilled, {skipped} already had a tourney_export doc.')


if __name__ == '__main__':
    main()
