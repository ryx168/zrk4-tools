"""Upload attachment bytes to Google Drive instead of R2.

Opt-in per mailbox (ATTACHMENTS_TO_DRIVE=1) - jimmyloong/ramin/etc. keep
using R2 for attachments unchanged. Reuses the SAME Drive account/OAuth app
already set up for raminandsons.com (xotourscanadaltd@gmail.com); only the
refresh token is needed, access tokens are minted on demand and never
persisted here.

Known failure mode from the ramin migration this is deliberately avoiding:
97 duplicate uploads because nothing checked for an existing file first.
Every upload here checks-by-name in the target folder before uploading.
"""
import json
import mimetypes
import os
import urllib.parse
import urllib.request

DRIVE_FILES = 'https://www.googleapis.com/drive/v3/files'
DRIVE_UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
ROOT_FOLDER_NAME = 'cloudmail-attachments'


def _post_form(url, data, headers=None):
    body = urllib.parse.urlencode(data).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _api(method, url, token, data=None, extra_headers=None):
    headers = {'Authorization': 'Bearer ' + token}
    headers.update(extra_headers or {})
    body = None
    if data is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(data).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


class DriveUploader:
    def __init__(self, client_id, client_secret, refresh_token):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._token = None
        self._folder_cache = {}

    def _access_token(self):
        if self._token:
            return self._token
        resp = _post_form(TOKEN_URL, {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'refresh_token': self.refresh_token,
            'grant_type': 'refresh_token',
        }, {'Content-Type': 'application/x-www-form-urlencoded'})
        token = resp.get('access_token')
        if not token:
            raise RuntimeError('drive token refresh failed: %s' % resp)
        self._token = token
        return token

    def _find_or_create_folder(self, name, parent_id=None):
        key = (parent_id, name)
        if key in self._folder_cache:
            return self._folder_cache[key]
        token = self._access_token()
        q = "name='%s' and mimeType='application/vnd.google-apps.folder' and trashed=false" % name.replace("'", "\\'")
        if parent_id:
            q += " and '%s' in parents" % parent_id
        else:
            q += " and 'root' in parents"
        url = DRIVE_FILES + '?' + urllib.parse.urlencode({'q': q, 'fields': 'files(id,name)'})
        result = _api('GET', url, token)
        files = result.get('files') or []
        if files:
            folder_id = files[0]['id']
        else:
            meta = {'name': name, 'mimeType': 'application/vnd.google-apps.folder'}
            if parent_id:
                meta['parents'] = [parent_id]
            created = _api('POST', DRIVE_FILES, token, data=meta)
            folder_id = created['id']
        self._folder_cache[key] = folder_id
        return folder_id

    def _find_existing_file(self, name, parent_id):
        token = self._access_token()
        q = "name='%s' and trashed=false and '%s' in parents" % (name.replace("'", "\\'"), parent_id)
        url = DRIVE_FILES + '?' + urllib.parse.urlencode({'q': q, 'fields': 'files(id,name)'})
        result = _api('GET', url, token)
        files = result.get('files') or []
        return files[0]['id'] if files else None

    def _multipart_create(self, token, filename, parent_id, data, ctype):
        boundary = '-------cloudmailupload'
        meta = json.dumps({'name': filename, 'parents': [parent_id]}).encode('utf-8')
        body = (
            ('--%s\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n' % boundary).encode('utf-8')
            + meta
            + ('\r\n--%s\r\nContent-Type: %s\r\n\r\n' % (boundary, ctype)).encode('utf-8')
            + data
            + ('\r\n--%s--' % boundary).encode('utf-8')
        )
        req = urllib.request.Request(
            DRIVE_UPLOAD + '?uploadType=multipart',
            data=body, method='POST',
            headers={
                'Authorization': 'Bearer ' + token,
                'Content-Type': 'multipart/related; boundary=' + boundary,
            })
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())['id']

    def upload(self, mailbox_local, filename, data, content_type=None):
        """Returns the Drive file id, uploading only if not already present
        under this mailbox's folder (checked by name - the dedup the ramin
        migration skipped). For content that is written once and never
        changes again (messages, attachments) - use upsert() for anything
        that gets rewritten on every sync (the index, the status file)."""
        token = self._access_token()
        root_id = self._find_or_create_folder(ROOT_FOLDER_NAME)
        mbox_id = self._find_or_create_folder(mailbox_local, root_id)

        existing = self._find_existing_file(filename, mbox_id)
        if existing:
            return existing

        ctype = content_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        return self._multipart_create(token, filename, mbox_id, data, ctype)

    def upsert(self, mailbox_local, filename, data, content_type=None):
        """Like upload(), but OVERWRITES the content if the file already
        exists instead of skipping - for files that change on every sync
        (search-index.json, sync-status.json). A plain upload() here would
        permanently freeze the index at whatever it was on first sync."""
        token = self._access_token()
        root_id = self._find_or_create_folder(ROOT_FOLDER_NAME)
        mbox_id = self._find_or_create_folder(mailbox_local, root_id)
        ctype = content_type or mimetypes.guess_type(filename)[0] or 'application/octet-stream'

        existing = self._find_existing_file(filename, mbox_id)
        if not existing:
            return self._multipart_create(token, filename, mbox_id, data, ctype)

        req = urllib.request.Request(
            DRIVE_UPLOAD + '/' + existing + '?uploadType=media',
            data=data, method='PATCH',
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': ctype})
        with urllib.request.urlopen(req, timeout=120) as r:
            json.loads(r.read())
        return existing


def from_env():
    client_id = os.environ.get('GOOGLE_CLIENT_ID')
    client_secret = os.environ.get('GOOGLE_CLIENT_SECRET')
    refresh_token = os.environ.get('GOOGLE_REFRESH_TOKEN')
    if not (client_id and client_secret and refresh_token):
        return None
    return DriveUploader(client_id, client_secret, refresh_token)
