#!/usr/bin/env python3
"""PULL a mailbox over IMAP into CloudMail's per-message JSON + search index.

Why pull, when maildir_to_cloudmail.py already works: that one reads the
maildir off local disk, so the whole pipeline - scripts, upload token, timers -
has to live on whichever box currently holds the mail. When the estate moved to
mail6 as primary, the rack1 sync silently went stale and the fix would have
been to install the entire pipeline a second time on mail6. This reads IMAP
instead, so the box holding the maildir is just a hostname in one variable.

Authentication is dovecot's MASTER USER: logging in as
`target@domain*hubmaster` with the master password opens any mailbox without
knowing or changing its own password. mail6 and mail7 both accept the same
credential.

IDS MUST MATCH maildir_to_cloudmail.py. The id is md5(messageId + the
JavaScript Date.toString() of the message date), the same as
generateEmailId() in sync-imap.js. Get this wrong and the pull does not update
the 12k messages already in R2, it writes a second copy of every one of them.

Attachments are uploaded from the bytes already fetched here, rather than by a
second pass over a maildir that this script never reads.

Usage:
  MAIL_PREFIX=ramin ACCOUNT_ID=ramin_raminandsons \
  IMAP_HOST=64.188.31.56 IMAP_MASTER_PASS_FILE=/root/.hubmaster-pass \
  UPLOAD_TOKEN=... python3 imap_to_cloudmail.py <mailbox> <outdir> <base-url>
"""
import email
import email.utils
import hashlib
import imaplib
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email import policy

MAILBOX = sys.argv[1]
OUT = sys.argv[2]
BASE = sys.argv[3].rstrip('/')

ACCOUNT_ID = os.environ.get('ACCOUNT_ID', 'it_xotours')
PREFIX = os.environ.get('MAIL_PREFIX', 'jimmyloong').strip('/')
ATT_PREFIX = PREFIX + '/attachments/'
MAX_ATTACHMENT = int(os.environ.get('MAX_ATTACHMENT_SIZE', 500 * 1024))
IMAP_HOST = os.environ.get('IMAP_HOST', '64.188.31.56')
IMAP_PORT = int(os.environ.get('IMAP_PORT', '993'))
MASTER_USER = os.environ.get('IMAP_MASTER_USER', 'hubmaster')
PASS_FILE = os.environ.get('IMAP_MASTER_PASS_FILE', '/root/.hubmaster-pass')
TOKEN = os.environ.get('UPLOAD_TOKEN', '')
# Fetching every message every run would be a pull in name only - the point is
# that steady state costs only new mail.
STATE_PATH = os.path.join(OUT, 'imap_state.json')

JS_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
             'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
JS_DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

CTYPES = {
    'pdf': 'application/pdf', 'png': 'image/png', 'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg', 'gif': 'image/gif', 'txt': 'text/plain',
    'ics': 'text/calendar', 'zip': 'application/zip',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}


def js_date_string(dt):
    """Node's Date.prototype.toString() with TZ=UTC. Must match
    maildir_to_cloudmail.py character for character or ids diverge."""
    return '%s %s %02d %04d %02d:%02d:%02d GMT+0000 (Coordinated Universal Time)' % (
        JS_DAYS[(dt.weekday() + 1) % 7], JS_MONTHS[dt.month - 1], dt.day, dt.year,
        dt.hour, dt.minute, dt.second)


def email_id(message_id, dt):
    h = hashlib.md5((message_id + js_date_string(dt)).encode('utf-8', 'replace')).hexdigest()
    return 'email-' + h[:12]


def sanitize(fn):
    """Must match sanitizeFilename() in sync-imap.js and push_attachments.py
    exactly, or attachments already in the bucket gain a second copy."""
    s = re.sub(r'[^a-z0-9.-]', '_', fn or 'attachment', flags=re.I)
    s = re.sub(r'_{2,}', '_', s)
    s = re.sub(r'\.{2,}', '.', s).strip('.') or 'attachment'
    return s[:100]


def normalize_folder(folder):
    f = folder.lower()
    if 'sent' in f:
        return 'sent'
    if 'draft' in f:
        return 'drafts'
    if 'trash' in f or 'bin' in f:
        return 'trash'
    if 'junk' in f or 'spam' in f:
        return 'junk'
    return 'inbox'


def addr_list(msg, header):
    out = []
    raw = msg.get_all(header)
    if not raw:
        return out
    for name, addr in email.utils.getaddresses([str(r) for r in raw]):
        if addr:
            out.append({'name': (name or '').strip(), 'email': addr.strip()})
    return out


def part_text(part):
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ''
    if payload is None:
        return ''
    charset = part.get_content_charset() or 'utf-8'
    try:
        return payload.decode(charset, 'replace')
    except (LookupError, UnicodeDecodeError):
        return payload.decode('utf-8', 'replace')


def upload(key, data, ctype='application/json'):
    req = urllib.request.Request(
        '%s/api/r2-put?key=%s' % (BASE, urllib.parse.quote(key, safe='/')),
        data=data, method='POST',
        headers={'Authorization': 'Bearer ' + TOKEN,
                 'X-Content-Type': ctype,
                 'Content-Type': 'application/octet-stream',
                 # Cloudflare 403s the default Python-urllib User-Agent at the
                 # edge, before the Function runs.
                 'User-Agent': 'xotours-mail-sync/1.0'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.status == 200
        except Exception as exc:
            if attempt == 2:
                print('  UPLOAD FAILED %s: %s' % (key, exc), file=sys.stderr)
                return False
            time.sleep(2 * (attempt + 1))
    return False


def load_state():
    try:
        with open(STATE_PATH, encoding='utf-8') as fh:
            s = json.load(fh)
            return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_PATH)


def id_from_header_bytes(raw):
    """(email_id, datetime) from a BODY.PEEK[HEADER.FIELDS ...] block.

    Uses the SAME parser as process_message. Parsing these headers by hand
    does not unfold a header continued onto the next line, which silently
    produced a different id for those messages and made every reconcile pass
    churn the same set forever. Returns (None, None) when there is no usable
    Date, which is what the callers already skip on.
    """
    msg = email.message_from_bytes(raw, policy=policy.default)
    raw_date = msg.get('Date')
    dt = None
    if raw_date:
        try:
            dt = email.utils.parsedate_to_datetime(str(raw_date))
        except (TypeError, ValueError):
            dt = None
    if dt is None:
        return None, None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    mid = (str(msg.get('Message-ID') or '')).strip() or str(int(dt.timestamp() * 1000))
    return email_id(mid, dt), dt


def list_folders(imap):
    typ, data = imap.list()
    if typ != 'OK':
        return []
    names = []
    for raw in data or []:
        if not raw:
            continue
        line = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
        # ( \HasNoChildren ) "." "INBOX.Sent"   -> take the quoted tail
        m = re.match(r'^\([^)]*\)\s+("[^"]*"|\S+)\s+(.*)$', line)
        if not m:
            continue
        name = m.group(2).strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        if name:
            names.append(name)
    return names


def process_message(raw_bytes, flags, folder_label, out_counts):
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)

    dt = None
    raw_date = msg.get('Date')
    if raw_date:
        try:
            dt = email.utils.parsedate_to_datetime(str(raw_date))
        except (TypeError, ValueError):
            dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)

    mid = (str(msg.get('Message-ID') or '')).strip() or str(int(dt.timestamp() * 1000))
    eid = email_id(mid, dt)

    text, html, atts = '', '', []
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        disp = (part.get('Content-Disposition') or '').lower()
        ctype = part.get_content_type()
        is_att = 'attachment' in disp or (part.get_filename() is not None)
        if is_att:
            raw_name = part.get_filename() or 'attachment'
            data = part.get_payload(decode=True) or b''
            entry = {
                'filename': raw_name,
                'size': len(data),
                'type': ctype,
                'url': None,
                'tooLarge': len(data) > MAX_ATTACHMENT,
                'synced': False,
            }
            # Upload from the bytes already in hand - there is no maildir to
            # go back to, which is the whole point of pulling.
            if data and len(data) <= MAX_ATTACHMENT:
                safe = sanitize(raw_name)
                objname = '%s-%s' % (eid, safe)
                ct = CTYPES.get(safe.split('.')[-1].lower(), ctype or 'application/octet-stream')
                if upload(ATT_PREFIX + objname, data, ct):
                    entry['url'] = '/attachments/' + objname
                    entry['synced'] = True
                    out_counts['attachments'] += 1
                else:
                    out_counts['att_failed'] += 1
            atts.append(entry)
            continue
        if ctype == 'text/plain' and not text:
            text = part_text(part)
        elif ctype == 'text/html' and not html:
            html = part_text(part)

    flagstr = ' '.join(flags)
    subject = str(msg.get('Subject') or '').strip() or '(No Subject)'
    frm = addr_list(msg, 'From')
    frm = frm[0] if frm else {'name': '', 'email': ''}
    iso = dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    preview = re.sub(r'\s+', ' ', text).strip()[:200]

    rec = {
        'id': eid,
        'messageId': mid,
        'from': frm,
        'to': addr_list(msg, 'To'),
        'subject': subject,
        'date': iso,
        'preview': preview,
        'body': text,
        'bodyHtml': html,
        'attachments': atts,
        'folder': normalize_folder(folder_label),
        'labels': [normalize_folder(folder_label)],
        # Maildir encodes these in the filename; IMAP has them as real flags.
        'read': '\\Seen' in flagstr,
        'starred': '\\Flagged' in flagstr,
        'hasAttachments': bool(atts),
        'inReplyTo': str(msg.get('In-Reply-To') or '') or None,
        'references': str(msg.get('References') or '') or None,
    }

    d = os.path.join(OUT, 'emails', dt.strftime('%Y'), dt.strftime('%m'))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, eid + '.json'), 'w', encoding='utf-8') as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=2)
    return eid


def rebuild_index():
    """From EVERY message on disk, not just this run's - exactly as the maildir
    parser does. An index of only the new mail would hide everything the
    earlier maildir syncs produced."""
    index = []
    for root, _d, files in os.walk(os.path.join(OUT, 'emails')):
        for name in files:
            if not name.endswith('.json'):
                continue
            fp = os.path.join(root, name)
            try:
                with open(fp, encoding='utf-8') as fh:
                    e = json.load(fh)
            except Exception:
                continue
            rel = os.path.relpath(fp, OUT).replace(os.sep, '/')
            frm = e.get('from') or {}
            index.append({
                'id': e['id'],
                'from': frm.get('email', ''),
                'fromName': frm.get('name') or frm.get('email', '').split('@')[0],
                'to': e.get('to', []),
                'subject': e.get('subject', '(No Subject)'),
                'preview': e.get('preview', ''),
                'date': e.get('date'),
                'folder': e.get('folder', 'inbox'),
                'labels': e.get('labels', ['inbox']),
                'account': ACCOUNT_ID,
                'path': '/' + rel,
                'hasAttachments': bool(e.get('attachments')),
                'attachmentCount': len(e.get('attachments') or []),
            })
    index.sort(key=lambda e: e['date'] or '', reverse=True)
    with open(os.path.join(OUT, 'search-index.json'), 'w', encoding='utf-8') as fh:
        json.dump({'emails': index,
                   'totalEmails': len(index),
                   'version': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')},
                  fh, ensure_ascii=False, indent=2)
    return len(index)


def main():
    if not TOKEN:
        print('  UPLOAD_TOKEN is not set', file=sys.stderr)
        return 1
    try:
        with open(PASS_FILE) as fh:
            master_pw = fh.read().strip()
    except OSError as exc:
        print('  cannot read %s: %s' % (PASS_FILE, exc), file=sys.stderr)
        return 1
    if not master_pw:
        print('  master password file is empty', file=sys.stderr)
        return 1

    os.makedirs(OUT, exist_ok=True)
    state = load_state()

    ctx = ssl.create_default_context()
    # These are internal hostnames/IPs on the mail mesh with per-host certs
    # that do not match the address dialled; the transport is already inside
    # WireGuard. Same posture as sync-imap.js.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx)
    imap.login('%s*%s' % (MAILBOX, MASTER_USER), master_pw)

    counts = {'new': 0, 'attachments': 0, 'att_failed': 0, 'failed': 0}
    folders = list_folders(imap)
    if not folders:
        folders = ['INBOX']
    print('  %s: %d folders on %s' % (MAILBOX, len(folders), IMAP_HOST))

    for folder in folders:
        try:
            typ, _ = imap.select('"%s"' % folder, readonly=True)
            if typ != 'OK':
                continue
        except Exception as exc:
            print('  skip %s: %s' % (folder, exc), file=sys.stderr)
            continue

        # UIDVALIDITY changing means the server renumbered the folder and every
        # stored UID is meaningless - treat it as a fresh folder rather than
        # silently skipping real mail.
        typ, uv = imap.status('"%s"' % folder, '(UIDVALIDITY)')
        uidvalidity = ''
        if typ == 'OK' and uv:
            m = re.search(r'UIDVALIDITY\s+(\d+)', uv[0].decode('utf-8', 'replace'))
            uidvalidity = m.group(1) if m else ''

        fstate = state.get(folder) or {}
        last_uid = fstate.get('last_uid', 0)
        if fstate.get('uidvalidity') and fstate.get('uidvalidity') != uidvalidity:
            print('  %s: UIDVALIDITY changed, refetching from 1' % folder)
            last_uid = 0

        typ, data = imap.uid('search', None, '(UID %d:*)' % (last_uid + 1))
        if typ != 'OK':
            continue
        uids = [u for u in (data[0].split() if data and data[0] else [])]
        # "UID n:*" always returns at least the highest message even when
        # nothing is new, so drop anything already seen.
        uids = [u for u in uids if int(u) > last_uid]
        if not uids:
            state[folder] = {'uidvalidity': uidvalidity, 'last_uid': last_uid}
            continue

        highest = last_uid
        for u in uids:
            try:
                typ, md = imap.uid('fetch', u, '(FLAGS RFC822)')
                if typ != 'OK' or not md or not md[0]:
                    continue
                raw = md[0][1]
                flagstr = md[0][0].decode('utf-8', 'replace') if md[0][0] else ''
                flags = re.findall(r'\\\w+', flagstr)
                process_message(raw, flags, folder, counts)
                counts['new'] += 1
                highest = max(highest, int(u))
            except Exception as exc:
                counts['failed'] += 1
                print('  FAILED uid %s in %s: %s' % (u, folder, exc), file=sys.stderr)
        state[folder] = {'uidvalidity': uidvalidity, 'last_uid': highest}
        # Save after every folder: a crash must not refetch everything.
        save_state(state)

    imap.logout()
    total = rebuild_index()
    print('  pulled %d new, %d attachments (%d failed), %d messages failed, index %d'
          % (counts['new'], counts['attachments'], counts['att_failed'], counts['failed'], total))
    return 0 if counts['failed'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
