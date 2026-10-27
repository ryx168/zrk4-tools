#!/usr/bin/env python3
"""Record current IMAP UIDs for every folder WITHOUT fetching any message
content. Writes the exact state-file shape fetch.py expects, so the first
real pull starts from "now" instead of walking the whole mailbox history.

Only runs when no state file exists yet (a fresh actions/cache, e.g. the
very first run, or a cache eviction). Touches nothing but UIDVALIDITY/UID
numbers - no subjects, no bodies, no addresses.

Usage: MAIL env vars same as fetch.py. seed.py <mailbox> <outdir>
"""
import imaplib
import json
import os
import re
import ssl
import sys

MAILBOX = sys.argv[1]
OUT = sys.argv[2]
STATE_PATH = os.path.join(OUT, 'imap_state.json')

IMAP_HOST = os.environ.get('IMAP_HOST', '64.188.31.56')
IMAP_PORT = int(os.environ.get('IMAP_PORT', '993'))
MASTER_USER = os.environ.get('IMAP_MASTER_USER', 'hubmaster')
PASS_FILE = os.environ.get('IMAP_MASTER_PASS_FILE')


def list_folders(imap):
    typ, data = imap.list()
    if typ != 'OK':
        return []
    names = []
    for raw in data or []:
        if not raw:
            continue
        line = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
        m = re.match(r'^\([^)]*\)\s+("[^"]*"|\S+)\s+(.*)$', line)
        if not m:
            continue
        name = m.group(2).strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        if name:
            names.append(name)
    return names


def main():
    if os.path.exists(STATE_PATH):
        print('  state already present, nothing to seed')
        return 0

    with open(PASS_FILE) as fh:
        master_pw = fh.read().strip()

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)
    imap.login('%s*%s' % (MAILBOX, MASTER_USER), master_pw)

    state = {}
    for folder in list_folders(imap) or ['INBOX']:
        try:
            typ, _ = imap.select('"%s"' % folder, readonly=True)
            if typ != 'OK':
                continue
            typ, uv = imap.status('"%s"' % folder, '(UIDVALIDITY)')
            uidvalidity = ''
            if typ == 'OK' and uv:
                m = re.search(r'UIDVALIDITY\s+(\d+)', uv[0].decode('utf-8', 'replace'))
                uidvalidity = m.group(1) if m else ''
            typ, data = imap.uid('search', None, 'ALL')
            uids = (data[0].split() if typ == 'OK' and data and data[0] else [])
            last_uid = max((int(u) for u in uids), default=0)
            state[folder] = {'uidvalidity': uidvalidity, 'last_uid': last_uid}
            print('  %s: seeded at uid %d' % (folder, last_uid))
        except Exception as exc:
            print('  skip %s: %s' % (folder, exc), file=sys.stderr)
    imap.logout()

    os.makedirs(OUT, exist_ok=True)
    with open(STATE_PATH, 'w', encoding='utf-8') as fh:
        json.dump(state, fh)
    print('  seeded %d folder(s), no message content fetched' % len(state))
    return 0


if __name__ == '__main__':
    sys.exit(main())
