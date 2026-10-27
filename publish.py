#!/usr/bin/env python3
"""Push the parsed message JSON and the index to R2.

This is what replaces the GitHub Actions sync. Nothing is committed and
nothing is deployed: the Pages Functions read these objects straight out of
the bucket, so new mail is visible as soon as this finishes.

Only CHANGED files are uploaded, tracked by content hash in uploaded.json -
re-sending 1,968 unchanged messages every 15 minutes would be most of the
cost of running this at all. The index is always sent last and always sent,
because it is the file that makes new messages visible, and sending it
before its messages would point the app at objects that are not there yet.

Usage:
  UPLOAD_TOKEN=... python3 push_json.py <outdir> <base-url>
"""
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request

OUT = sys.argv[1]
BASE = sys.argv[2].rstrip('/')
TOKEN = os.environ.get('UPLOAD_TOKEN', '')
PREFIX = os.environ.get('MAIL_PREFIX', 'jimmyloong').strip('/') + '/'
LOCAL = os.environ.get('MAIL_PREFIX', 'jimmyloong').strip('/')
MANIFEST = os.path.join(OUT, 'uploaded.json')
# This runner never holds the mailbox's full history locally (seeded from
# current UIDs forward, nothing older fetched - see seed.py), so a local
# rebuild_index() here only ever covers "since the bridge started", not the
# real mailbox. Uploading that would overwrite the complete index the
# primary puller maintains from its own full local copy - exactly backwards
# for a job whose point is to help during an outage, not blank the archive
# the moment it runs. Message content stays fully additive either way.
# NOT set for mailboxes that have no other sync path (the 9 new ones) -
# there, this bridge IS the whole history, so its index is already complete.
SKIP_INDEX = os.environ.get('SKIP_INDEX') == '1'

# Opt-in: push everything (messages, index, status) to Google Drive instead
# of R2 - same flag fetch.py uses for attachments, so one mailbox is either
# fully on Drive or fully on R2, never a mix.
ALL_TO_DRIVE = os.environ.get('ATTACHMENTS_TO_DRIVE') == '1'
_drive = None
if ALL_TO_DRIVE:
    import drive_upload
    _drive = drive_upload.from_env()
    if _drive is None:
        print('  ATTACHMENTS_TO_DRIVE=1 but GOOGLE_* credentials are missing', file=sys.stderr)


def upload(key, data, ctype='application/json'):
    req = urllib.request.Request(
        '%s/api/r2-put?key=%s' % (BASE, urllib.parse.quote(key, safe='/')),
        data=data, method='POST',
        headers={'Authorization': 'Bearer ' + TOKEN,
                 'X-Content-Type': ctype,
                 'Content-Type': 'application/octet-stream',
                 # Cloudflare 403s the default Python-urllib User-Agent at
                 # the edge, before the Function runs.
                 'User-Agent': 'xotours-mail-sync/1.0'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status == 200
        except Exception as exc:
            if attempt == 2:
                print('  UPLOAD FAILED %s: %s' % (key, exc), file=sys.stderr)
                return False
            time.sleep(2 * (attempt + 1))
    return False


def main():
    manifest = {}
    if os.path.exists(MANIFEST):
        try:
            with open(MANIFEST, encoding='utf-8') as fh:
                manifest = json.load(fh)
        except Exception:
            manifest = {}

    sent = unchanged = failed = 0
    emails_root = os.path.join(OUT, 'emails')
    for root, _d, files in os.walk(emails_root):
        for name in sorted(files):
            if not name.endswith('.json'):
                continue
            fp = os.path.join(root, name)
            rel = os.path.relpath(fp, OUT).replace(os.sep, '/')
            with open(fp, 'rb') as fh:
                data = fh.read()
            digest = hashlib.sha256(data).hexdigest()
            if manifest.get(rel) == digest:
                unchanged += 1
                continue
            ok = False
            if ALL_TO_DRIVE and _drive is not None:
                try:
                    # Flat filename - a message JSON never changes once
                    # written, so dedup-by-name (upload(), not upsert()) is
                    # correct and matches how attachments already behave.
                    _drive.upload(LOCAL, name, data, 'application/json')
                    ok = True
                except Exception as exc:
                    print('  DRIVE UPLOAD FAILED %s: %s' % (rel, exc), file=sys.stderr)
            else:
                ok = upload(PREFIX + rel, data)
            if ok:
                manifest[rel] = digest
                sent += 1
            else:
                failed += 1

    # Save the manifest before the index, so a crash here does not make the
    # next run re-send everything.
    with open(MANIFEST, 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh)

    idx = os.path.join(OUT, 'search-index.json')
    idx_ok = False
    if SKIP_INDEX:
        pass
    elif os.path.exists(idx):
        with open(idx, 'rb') as fh:
            idx_data = fh.read()
        if ALL_TO_DRIVE and _drive is not None:
            try:
                _drive.upsert(LOCAL, 'search-index.json', idx_data, 'application/json')
                idx_ok = True
            except Exception as exc:
                print('  DRIVE INDEX UPLOAD FAILED: %s' % exc, file=sys.stderr)
        else:
            idx_ok = upload(PREFIX + 'search-index.json', idx_data)

    status = {
        'last_sync': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'messages_sent': sent,
        'messages_unchanged': unchanged,
        'messages_failed': failed,
        'index_uploaded': idx_ok,
        'index_skipped': SKIP_INDEX,
    }
    status_data = json.dumps(status).encode('utf-8')
    if ALL_TO_DRIVE and _drive is not None:
        try:
            _drive.upsert(LOCAL, 'sync-status.json', status_data, 'application/json')
        except Exception as exc:
            print('  DRIVE STATUS UPLOAD FAILED: %s' % exc, file=sys.stderr)
    else:
        upload(PREFIX + 'sync-status.json', status_data)

    print('  json: sent %d, unchanged %d, failed %d, index %s'
          % (sent, unchanged, failed,
             'skipped' if SKIP_INDEX else ('ok' if idx_ok else 'FAILED')))
    return 0 if failed == 0 and (idx_ok or SKIP_INDEX) else 1


if __name__ == '__main__':
    sys.exit(main())
