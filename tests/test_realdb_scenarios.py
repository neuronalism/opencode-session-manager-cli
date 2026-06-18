"""Real-DB scenario test: folder-new-session import + deletion propagation."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

DB = Path(r"C:\Users\WH\AppData\Local\Temp\ocsm-realdb-test\opencode.db")
PROJ = Path(r"C:\Users\WH\AppData\Local\Temp\ocsm-realdb-test\sandbox-proj")
RAW = PROJ / ".opencode" / "raw_conversations"


def run_sync(*extra):
    cmd = ["uv", "run", "ocsm", "--db", str(DB), "sync", "project",
           "--from", str(PROJ), "--on-conflict", "newer", "-y"] + list(extra)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    # filter noise
    lines = [ln for ln in (r.stdout or "").splitlines()
             if not any(s in ln for s in
                        ["could not verify", "Verifying", "Backup:", "Delete the backup", "opencode db"])]
    print("\n".join(lines))
    return r.returncode, r.stdout


def db_count(sid):
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT id, directory, project_id, title FROM session WHERE id=?", (sid,)).fetchone()
    conn.close()
    return dict(r) if r else None


print("=" * 60)
print("Step 4: folder-side NEW session -> should import into DB")
print("=" * 60)
fake_sid = "ses_FAKE_FOLDER_NEW_001"
fake = {
    "session": {
        "id": fake_sid, "project_id": "global", "parent_id": None,
        "slug": "fake-new", "directory": "D:/placeholder",
        "title": "fake folder-only session", "version": "1",
        "time_created": 1750000000000, "time_updated": 1750000000000,
        "summary_diffs": None,
    },
    "messages": [
        {"id": "fake-m1", "session_id": fake_sid, "time_created": 1750000000000,
         "time_updated": 1750000000000, "data": json.dumps({"role": "user", "text": "hello"})}
    ],
    "parts": [],
}
(RAW / "fake-new-session.json").write_text(json.dumps(fake, indent=2), encoding="utf-8")
print("created", fake_sid)

rc, _ = run_sync()
row = db_count(fake_sid)
print("\nDB row after import:", row)
assert row is not None, "FAIL: fake session not imported"
assert row["directory"] == str(PROJ.resolve()).replace("\\", "/") or row["directory"] == str(PROJ.resolve()), \
    f"FAIL: directory not rewritten: {row['directory']!r}"
assert row["project_id"] == "global", f"FAIL: project_id not global: {row['project_id']!r}"
print("[PASS] folder -> DB import on real DB")

print()
print("=" * 60)
print("Step 5: DELETION propagation (delete fake from DB -> remove from folder)")
print("=" * 60)
# remove fake from DB
conn = sqlite3.connect(str(DB))
conn.execute("DELETE FROM message WHERE session_id=?", (fake_sid,))
conn.execute("DELETE FROM session WHERE id=?", (fake_sid,))
conn.commit()
conn.close()
print("deleted", fake_sid, "from DB")
print("folder file exists before sync:", (RAW / "fake-new-session.json").is_file())

rc, _ = run_sync()
print("folder file exists after sync:", (RAW / "fake-new-session.json").is_file())
assert not (RAW / "fake-new-session.json").is_file(), "FAIL: file not removed by deletion propagation"
print("[PASS] DB-side deletion propagated to folder on real DB")

print()
print("=" * 60)
print("Step 6: DELETION propagation (delete a real session from folder -> remove from DB)")
print("=" * 60)
# pick a real root session present in folder
root_file = RAW / "ses_13035b943ffeXIYb2Snivw6fLX.json"
target_sid = "ses_13035b943ffeXIYb2Snivw6fLX"
assert root_file.is_file(), "root file missing"
root_file.unlink()
# also remove its subtree dir
subtree = RAW / target_sid
import shutil
if subtree.is_dir():
    shutil.rmtree(subtree)
print("removed", target_sid, "from folder (file + subtree)")

before = db_count(target_sid)
print("DB row before sync:", "present" if before else "absent")

rc, _ = run_sync()
after = db_count(target_sid)
print("DB row after sync:", "present" if after else "absent")
assert after is None, "FAIL: DB row not removed by folder-side deletion propagation"
# check its subtree children also gone
conn = sqlite3.connect(str(DB))
child_count = conn.execute("SELECT COUNT(*) FROM session WHERE parent_id=?", (target_sid,)).fetchone()[0]
conn.close()
print("remaining children of deleted root:", child_count)
assert child_count == 0, "FAIL: children not cascaded"
print("[PASS] folder-side deletion propagated to DB (+ subtree cascade)")

print()
print("=" * 60)
print("ALL REAL-DB SCENARIO TESTS PASSED")
print("=" * 60)
