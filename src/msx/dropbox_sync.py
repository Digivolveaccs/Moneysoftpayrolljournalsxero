"""Cloud-fallback source: mirror new report files from Dropbox into a local
folder using the Dropbox HTTP API (stdlib only), so a runner with no Dropbox
desktop client (a VPS, a GitHub Actions job, a Claude Code Routine) can feed
the same discovery/parse/post pipeline by pointing ``pdf_root`` at the
mirror.

Cursor-based: ``files/list_folder`` once, then ``files/list_folder/continue``
with the stored cursor on every run, downloading only new or changed
``.pdf``/``.txt`` files under the watched folder. Deleted entries are
mirrored as deletions. The cursor is persisted after every successful pass
(write-ahead: a crash mid-pass simply re-lists from the old cursor).

Auth: a Dropbox app with ``files.metadata.read`` + ``files.content.read``
and an offline (refresh) token; the refresh token is long-lived and is not
rotated on use. Config::

    "dropbox": {"app_key": "...", "app_secret": "" (blank for PKCE apps),
                "refresh_token_file": "~/.config/msx/dropbox_refresh",
                "remote_root": "/Apps/msx-poster/inbox",
                "cursor_file": "~/.config/msx/dropbox_cursor",
                "path_root": "" (team space namespace id, if needed)}

The remote root should be an APP FOLDER (``/Apps/<app>/...``) that the
Mac-side filing step copies each filed PDF into, so the cloud app never
holds full-Dropbox access. This module is the fallback path and is
normally unused.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"
WANTED = (".pdf", ".txt")


class DropboxError(Exception):
    pass


class UrllibTransport:
    def request(self, method, url, headers=None, data=None, timeout=120):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()


class DropboxClient:
    def __init__(self, cfg, *, transport=None):
        self.cfg = cfg
        self.transport = transport or UrllibTransport()
        self._access = None

    def _refresh_token(self):
        path = os.path.expanduser(self.cfg["refresh_token_file"])
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()

    def _access_token(self):
        if self._access:
            return self._access
        body = {"grant_type": "refresh_token",
                "refresh_token": self._refresh_token(),
                "client_id": self.cfg["app_key"]}
        if self.cfg.get("app_secret"):
            body["client_secret"] = self.cfg["app_secret"]
        status, _, raw = self.transport.request(
            "POST", TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=urllib.parse.urlencode(body).encode())
        if status != 200:
            raise DropboxError(f"token refresh failed: {status} "
                               f"{raw[:200].decode(errors='replace')}")
        self._access = json.loads(raw.decode())["access_token"]
        return self._access

    def _headers(self, extra=None):
        h = {"Authorization": "Bearer " + self._access_token()}
        if self.cfg.get("path_root"):
            h["Dropbox-API-Path-Root"] = json.dumps(
                {".tag": "namespace_id", "namespace_id": self.cfg["path_root"]})
        h.update(extra or {})
        return h

    def rpc(self, endpoint, payload):
        status, headers, raw = self.transport.request(
            "POST", f"{API}/{endpoint}",
            headers=self._headers({"Content-Type": "application/json"}),
            data=json.dumps(payload).encode())
        if status == 401:
            self._access = None
            status, headers, raw = self.transport.request(
                "POST", f"{API}/{endpoint}",
                headers=self._headers({"Content-Type": "application/json"}),
                data=json.dumps(payload).encode())
        if status == 429:
            raise DropboxError("rate limited - retry later "
                               f"(Retry-After {headers.get('Retry-After')})")
        if status != 200:
            raise DropboxError(f"{endpoint} -> {status}: "
                               f"{raw[:300].decode(errors='replace')}")
        return json.loads(raw.decode() or "{}")

    def download(self, path, dest):
        status, _, raw = self.transport.request(
            "POST", f"{CONTENT}/files/download",
            headers=self._headers({"Dropbox-API-Arg": json.dumps({"path": path})}))
        if status != 200:
            raise DropboxError(f"download {path} -> {status}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, dest)
        return len(raw)


def _cursor_path(cfg):
    return os.path.expanduser(cfg.get("cursor_file", "~/.config/msx/dropbox_cursor"))


def sync(cfg, local_root, *, transport=None, log=lambda m: None):
    """Mirror new/changed files from cfg['remote_root'] into local_root.
    Returns {'downloaded': n, 'deleted': n, 'cursor': str}."""
    client = DropboxClient(cfg, transport=transport)
    remote_root = cfg["remote_root"].rstrip("/")
    cpath = _cursor_path(cfg)
    cursor = None
    if os.path.exists(cpath):
        with open(cpath, encoding="utf-8") as fh:
            cursor = fh.read().strip() or None
    downloaded = deleted = 0
    while True:
        if cursor:
            res = client.rpc("files/list_folder/continue", {"cursor": cursor})
        else:
            res = client.rpc("files/list_folder",
                             {"path": remote_root, "recursive": True,
                              "include_deleted": True})
        for e in res.get("entries", []):
            tag = e.get(".tag")
            rel = (e.get("path_display") or e.get("path_lower") or "")
            if not rel.lower().startswith(remote_root.lower() + "/"):
                continue
            rel = rel[len(remote_root) + 1:]
            dest = os.path.join(os.path.expanduser(local_root), rel)
            if tag == "file" and rel.lower().endswith(WANTED):
                size = client.download(e["path_lower"], dest)
                downloaded += 1
                log(f"downloaded {rel} ({size} bytes)")
            elif tag == "deleted" and os.path.exists(dest):
                os.unlink(dest)
                deleted += 1
                log(f"removed {rel}")
        cursor = res.get("cursor", cursor)
        os.makedirs(os.path.dirname(cpath) or ".", exist_ok=True)
        tmp = cpath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(cursor or "")
        os.replace(tmp, cpath)
        if not res.get("has_more"):
            break
    return {"downloaded": downloaded, "deleted": deleted, "cursor": cursor}
