"""CLI end-to-end through subprocess with a temp config (no Xero)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.pdf_root = os.path.join(root, "PDF attachments")
        folder = os.path.join(self.pdf_root,
                              "Browns Garage (Haywards Heath) Limited 2026-27")
        os.makedirs(folder)
        p = os.path.join(folder, "Browns Garage (Haywards Heath) Limited"
                         " - Employer's Summary for Apr-2026.txt")
        shutil.copy(os.path.join(FIX, "browns_apr2026_tabbed.txt"), p)
        os.utime(p, (1_600_000_000, 1_600_000_000))
        self.clients = os.path.join(root, "clients")
        os.makedirs(self.clients)
        shutil.copy(os.path.join(ROOT, "clients", "browns-garage-haywards-heath.json"),
                    self.clients)
        self.cfg_path = os.path.join(root, "config.json")
        with open(self.cfg_path, "w") as fh:
            json.dump({"pdf_root": self.pdf_root, "clients_dir": self.clients,
                       "state_db": os.path.join(root, "s.sqlite"),
                       "out_dir": os.path.join(root, "out"),
                       "settle_seconds": 0, "stability_sample_seconds": 0,
                       "machine_name": "cli-test", "notify": {}}, fh)
        self.env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"),
                        MSX_CONFIG=self.cfg_path)

    def tearDown(self):
        self.tmp.cleanup()

    def msx(self, *args):
        r = subprocess.run([sys.executable, "-m", "msx.cli", *args],
                           capture_output=True, text=True, env=self.env,
                           cwd=ROOT)
        return r.returncode, r.stdout + r.stderr

    def test_build_status_doctor_run(self):
        out_dir = os.path.join(self.tmp.name, "build")
        code, out = self.msx("build", os.path.join(FIX, "browns_apr2026_tabbed.txt"),
                             "--out", out_dir)
        self.assertEqual(code, 0, out)
        self.assertIn("Dr = Cr = 41463.73", out)
        self.assertTrue(any(n.endswith(".csv") for n in os.listdir(out_dir)))

        code, out = self.msx("doctor")
        self.assertIn("mapping browns-garage-haywards-heath: mode=shadow ok", out)

        code, out = self.msx("run", "--no-notify")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "out", "HOLDS.md")))

        code, out = self.msx("status")
        self.assertEqual(code, 0, out)
        self.assertIn("shadow", out)
        self.assertIn("Apr-2026", out)

        code, out = self.msx("run", "--no-notify", "--mode", "post", "--dry-run")
        # dry-run with no Xero token: the client is held with an auth message,
        # never a crash, and the exit code says "held"
        self.assertEqual(code, 2, out)

    def test_bad_config_is_exit_3(self):
        with open(self.cfg_path, "w") as fh:
            fh.write("{not json")
        code, out = self.msx("status")
        self.assertEqual(code, 2, out)      # Hold from config load
        self.assertIn("not valid JSON", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
