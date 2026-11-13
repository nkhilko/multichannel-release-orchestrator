"""Coordinate an approved release across deterministic mock channels."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3


def freeze(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


class MockChannels:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS delivered (key TEXT PRIMARY KEY, body TEXT)")

    def send(self, key, body, fault=None):
        if fault == "unavailable":
            return
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR IGNORE INTO delivered VALUES (?, ?)", (key, body))
        if fault == "lost-receipt":
            raise ConnectionError("Simulated missing response")

    def read(self, key):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT body FROM delivered WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


class Release:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS releases (id TEXT PRIMARY KEY, manifest TEXT, reviewer TEXT)")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                release TEXT, channel TEXT, body TEXT, state TEXT, attempts INTEGER,
                PRIMARY KEY(release, channel)
            )""")

    def approve(self, release_id, outputs, reviewer, expected_digest):
        manifest = freeze(outputs)
        if not release_id.strip() or not reviewer.strip() or not outputs:
            raise ValueError("Release identity, channel outputs and reviewer are required")
        if sha256(manifest.encode()).hexdigest() != expected_digest:
            raise ValueError("The channel outputs changed after review")
        if any(not channel.strip() or not isinstance(body, dict) or not body for channel, body in outputs.items()):
            raise ValueError("Each named channel requires a payload")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO releases VALUES (?, ?, ?)", (release_id, manifest, reviewer))
            db.executemany("INSERT INTO jobs VALUES (?, ?, ?, 'queued', 0)",
                           [(release_id, channel, freeze(body)) for channel, body in outputs.items()])

    def run(self, release_id, provider, faults=None):
        faults = faults or {}
        with closing(sqlite3.connect(self.path)) as db:
            jobs = db.execute("SELECT channel, body FROM jobs WHERE release = ? ORDER BY channel",
                              (release_id,)).fetchall()
        if not jobs:
            raise ValueError("Expected an approved release")
        for channel, body in jobs:
            key = sha256(freeze([release_id, channel, body]).encode()).hexdigest()
            with closing(sqlite3.connect(self.path)) as db, db:
                claimed = db.execute("""UPDATE jobs SET state = 'attempting', attempts = attempts + 1
                    WHERE release = ? AND channel = ? AND state IN ('queued', 'retryable')""",
                    (release_id, channel)).rowcount
            if claimed:
                try:
                    provider.send(key, body, faults.get(channel))
                except ConnectionError:
                    continue
            self.verify(release_id, channel, provider, key, body, allow_retry=bool(claimed))
        return self.status(release_id)

    def verify(self, release_id, channel, provider, key, expected, *, allow_retry=False):
        actual = provider.read(key)
        if actual is None and not allow_retry:
            return
        state = "retryable" if actual is None else ("verified" if actual == expected else "conflict")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""UPDATE jobs SET state = ? WHERE release = ? AND channel = ?
                AND state = 'attempting'""", (state, release_id, channel))

    def status(self, release_id):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT channel, state, attempts FROM jobs WHERE release = ? ORDER BY channel",
                              (release_id,)).fetchall()
        return {"complete": bool(rows) and all(state == "verified" for _, state, _ in rows),
                "channels": {channel: {"state": state, "attempts": attempts} for channel, state, attempts in rows}}


if __name__ == "__main__":
    from tempfile import TemporaryDirectory

    outputs = {
        "storefront": {"title": "Paper Shapes", "product": "synthetic-print-01"},
        "newsletter": {"subject": "A new geometric print"},
        "social": {"caption": "A study in lines and colour"},
    }
    with TemporaryDirectory() as folder:
        root = Path(folder)
        release = Release(root / "release.sqlite")
        mock = MockChannels(root / "channels.sqlite")
        release.approve("sample-release", outputs, "demo-reviewer", sha256(freeze(outputs).encode()).hexdigest())
        first = release.run("sample-release", mock, {"newsletter": "lost-receipt", "social": "unavailable"})
        resumed = Release(release.path).run("sample-release", MockChannels(mock.path))
        print(json.dumps({"initial": first, "after_restart": resumed}, indent=2))
