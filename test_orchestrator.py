from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from orchestrator import freeze, MockChannels, Release

PAYLOADS = {"shop": {"title": "Paper Shapes"}, "feed": {"caption": "Example"}}
DIGEST = sha256(freeze(PAYLOADS).encode()).hexdigest()


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        self.release = Release(root / "release.sqlite")
        self.provider = MockChannels(root / "channels.sqlite")

    def test_unapproved_or_changed_manifest_cannot_publish(self):
        with self.assertRaises(ValueError):
            self.release.run("sample", self.provider)
        with self.assertRaises(ValueError):
            self.release.approve("sample", {"shop": {"title": "Changed"}}, "test", DIGEST)

    def test_restart_reconciles_accepted_write_and_retries_only_failed_channel(self):
        self.release.approve("sample", PAYLOADS, "test", DIGEST)
        first = self.release.run("sample", self.provider, {"shop": "lost-receipt", "feed": "unavailable"})
        self.assertFalse(first["complete"])
        self.assertEqual(first["channels"]["shop"]["state"], "attempting")
        self.assertEqual(first["channels"]["feed"]["state"], "retryable")
        restarted = Release(self.release.path)
        result = restarted.run("sample", MockChannels(self.provider.path))
        self.assertTrue(result["complete"])
        self.assertEqual(result["channels"]["shop"]["attempts"], 1)
        self.assertEqual(result["channels"]["feed"]["attempts"], 2)
        self.assertEqual(restarted.run("sample", self.provider), result)
        with closing(sqlite3.connect(self.provider.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM delivered").fetchone()[0], 2)

    def test_conflicting_readback_does_not_complete(self):
        self.release.approve("sample", PAYLOADS, "test", DIGEST)
        key = sha256(freeze(["sample", "shop", freeze(PAYLOADS["shop"])]).encode()).hexdigest()
        self.provider.send(key, freeze({"title": "Unapproved"}))
        state = self.release.run("sample", self.provider)
        self.assertEqual(state["channels"]["shop"]["state"], "conflict")
        self.assertFalse(state["complete"])

    def test_completed_receipt_rejects_replacement_provider(self):
        self.release.approve("sample", PAYLOADS, "test", DIGEST)
        self.assertTrue(self.release.run("sample", self.provider)["complete"])
        substitute = MockChannels(self.provider.path.with_name("replacement.sqlite"))
        with self.assertRaisesRegex(ValueError, "another provider"):
            self.release.run("sample", substitute)
        self.assertTrue(Release(self.release.path).run("sample", MockChannels(self.provider.path))["complete"])

    def test_unknown_pre_write_interruption_stays_held_after_restart(self):
        class Ambiguous(MockChannels):
            def send(self, key, body, fault=None):
                raise ConnectionError("Synthetic interruption before write")

        self.release.approve("sample", PAYLOADS, "test", DIGEST)
        self.release.run("sample", Ambiguous(self.provider.path))
        result = Release(self.release.path).run("sample", self.provider)
        self.assertFalse(result["complete"])
        for record in result["channels"].values():
            self.assertEqual(record, {"state": "attempting", "attempts": 1})
