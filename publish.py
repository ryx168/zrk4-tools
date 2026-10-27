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
TOKEN = os.environ['UPLOAD_TOKEN']
PREFIX = os.environ.get('MAIL_PREFIX', 'jimmyloong').strip('/') + '/'
MANIFEST = os.path.join(OUT, 'uploaded.json')


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
            if upload(PREFIX + rel, data):
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
    if os.path.exists(idx):
        with open(idx, 'rb') as fh:
            idx_ok = upload(PREFIX + 'search-index.json', fh.read())

    status = {
        'last_sync': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'messages_sent': sent,
        'messages_unchanged': unchanged,
        'messages_failed': failed,
        'index_uploaded': idx_ok,
    }
    upload(PREFIX + 'sync-status.json', json.dumps(status).encode('utf-8'))

    print('  json: sent %d, unchanged %d, failed %d, index %s'
          % (sent, unchanged, failed, 'ok' if idx_ok else 'FAILED'))
    return 0 if failed == 0 and idx_ok else 1


if __name__ == '__main__':
    sys.exit(main())
