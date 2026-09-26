import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from msx import dropbox_sync  # noqa: E402


class FakeDropbox:
    def __init__(self):
        self.calls = []
        self.pages = []
        self.files = {}

    def request(self, method, url, headers=None, data=None, timeout=120):
        self.calls.append((url, headers, data))
        if url.endswith("/oauth2/token"):
            return 200, {}, json.dumps({"access_token": "AT"}).encode()
        if url.endswith("/files/list_folder") or url.endswith("/list_folder/continue"):
            page = self.pages.pop(0)
            return 200, {}, json.dumps(page).encode()
        if url.endswith("/files/download"):
            arg = json.loads(headers["Dropbox-API-Arg"])
            return 200, {}, self.files[arg["path"]]
        raise AssertionError(url)


class DropboxSyncTests(unittest.TestCase):
    def test_initial_then_incremental(self):
        with tempfile.TemporaryDirectory() as d:
            rt = os.path.join(d, "refresh")
            with open(rt, "w") as fh:
                fh.write("RT")
            cfg = {"app_key": "k", "refresh_token_file": rt,
                   "remote_root": "/Apps/msx-poster/inbox",
                   "cursor_file": os.path.join(d, "cursor")}
            fake = FakeDropbox()
            fake.files["/apps/msx-poster/inbox/acme ltd 2026-27/acme ltd - employer's summary for apr-2026.pdf"] = b"%PDF-1.4 x"
            fake.pages = [
                {"entries": [
                    {".tag": "folder", "path_display": "/Apps/msx-poster/inbox/Acme Ltd 2026-27"},
                    {".tag": "file", "path_display": "/Apps/msx-poster/inbox/Acme Ltd 2026-27/Acme Ltd - Employer's Summary for Apr-2026.pdf",
                     "path_lower": "/apps/msx-poster/inbox/acme ltd 2026-27/acme ltd - employer's summary for apr-2026.pdf"},
                    {".tag": "file", "path_display": "/Apps/msx-poster/inbox/notes.docx",
                     "path_lower": "/apps/msx-poster/inbox/notes.docx"},
                ], "cursor": "C1", "has_more": True},
                {"entries": [], "cursor": "C2", "has_more": False},
            ]
            local = os.path.join(d, "mirror")
            r = dropbox_sync.sync(cfg, local, transport=fake)
            self.assertEqual(r["downloaded"], 1)
            self.assertEqual(r["cursor"], "C2")
            self.assertTrue(os.path.exists(os.path.join(
                local, "Acme Ltd 2026-27", "Acme Ltd - Employer's Summary for Apr-2026.pdf")))
            self.assertFalse(os.path.exists(os.path.join(local, "notes.docx")))
            self.assertTrue(fake.calls[1][0].endswith("/files/list_folder"))
            # second run continues from the stored cursor and mirrors a delete
            fake.pages = [{"entries": [
                {".tag": "deleted", "path_display": "/Apps/msx-poster/inbox/Acme Ltd 2026-27/Acme Ltd - Employer's Summary for Apr-2026.pdf",
                 "path_lower": "/apps/msx-poster/inbox/acme ltd 2026-27/acme ltd - employer's summary for apr-2026.pdf"}],
                "cursor": "C3", "has_more": False}]
            r2 = dropbox_sync.sync(cfg, local, transport=fake)
            self.assertEqual(r2["deleted"], 1)
            self.assertTrue(fake.calls[-1][0].endswith("/list_folder/continue"))
            self.assertEqual(json.loads(fake.calls[-1][2])["cursor"], "C2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
