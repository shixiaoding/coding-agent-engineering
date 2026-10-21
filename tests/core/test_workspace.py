#!/usr/bin/env python3

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "scripts" / "coding-rules-loop"
sys.path.insert(0, str(CORE_ROOT))

from workspace import calculate_task_diff_hash, collect_changes, file_hash_map, snapshot_workspace  # noqa: E402


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repository), *arguments], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name)
        git(self.repository, "init", "-q")
        git(self.repository, "config", "user.name", "fixture")
        git(self.repository, "config", "user.email", "fixture@example.com")
        git(self.repository, "config", "core.filemode", "true")
        (self.repository / "Main.java").write_text("class Main {}\n")
        (self.repository / "existing.txt").write_text("base\n")
        (self.repository / "Delete.java").write_text("class Delete {}\n")
        (self.repository / "Rename.java").write_text("class Rename {}\n")
        script = self.repository / "run.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o644)
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-qm", "fixture")

    def tearDown(self):
        self.temporary.cleanup()

    def test_baseline_excludes_unchanged_preexisting_dirty_files(self):
        (self.repository / "existing.txt").write_text("user change\n")
        (self.repository / "before.tmp").write_text("already here\n")
        baseline = snapshot_workspace(str(self.repository))

        (self.repository / "Main.java").write_text("class Main { int value; }\n")
        (self.repository / "Created.java").write_text("class Created {}\n")
        result = collect_changes(str(self.repository), baseline, ["Main.java"])

        self.assertEqual(["Created.java", "Main.java"], [change["path"] for change in result["changes"]])
        self.assertFalse(result["contains_existing_changes"])

        (self.repository / "existing.txt").write_text("task updated user change\n")
        result = collect_changes(str(self.repository), baseline, ["Main.java", "existing.txt"])
        existing = next(change for change in result["changes"] if change["path"] == "existing.txt")
        self.assertTrue(existing["existed_before_task"])
        self.assertTrue(result["contains_existing_changes"])

    def test_hash_is_stable_for_different_file_order_and_locates_changed_file(self):
        baseline = snapshot_workspace(str(self.repository))
        (self.repository / "Main.java").write_text("class Main { int value; }\n")
        (self.repository / "Created.java").write_text("class Created {}\n")
        changes = collect_changes(str(self.repository), baseline, ["Main.java", "Created.java"])["changes"]

        first = calculate_task_diff_hash(changes, "rules-1")
        second = calculate_task_diff_hash(list(reversed(changes)), "rules-1")
        hashes = file_hash_map(changes)

        self.assertEqual(first, second)
        self.assertEqual({"Created.java", "Main.java"}, set(hashes))
        self.assertNotEqual(hashes["Created.java"], hashes["Main.java"])
        self.assertNotEqual(first, calculate_task_diff_hash(changes, "rules-2"))

    def test_deleted_renamed_and_mode_changes_are_fingerprinted(self):
        baseline = snapshot_workspace(str(self.repository))
        (self.repository / "Delete.java").unlink()
        os.rename(self.repository / "Rename.java", self.repository / "Renamed.java")
        (self.repository / "run.sh").chmod(0o755)
        git(self.repository, "add", "-A")

        changes = collect_changes(str(self.repository), baseline)["changes"]
        by_path = {change["path"]: change for change in changes}

        self.assertIn("Delete.java", by_path)
        self.assertIn("Renamed.java", by_path)
        self.assertEqual("Rename.java", by_path["Renamed.java"]["original_path"])
        self.assertEqual(0o755, stat.S_IMODE(by_path["run.sh"]["mode"]))
        self.assertEqual(3, len({change["file_hash"] for change in changes}))

    def test_changes_committed_during_task_remain_relative_to_baseline_head(self):
        baseline = snapshot_workspace(str(self.repository))
        (self.repository / "Main.java").write_text("class Main { int committed; }\n")
        (self.repository / "Committed.java").write_text("class Committed {}\n")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-qm", "task change")

        result = collect_changes(str(self.repository), baseline, ["Main.java", "Committed.java"])
        paths = [change["path"] for change in result["changes"]]

        self.assertEqual(["Committed.java", "Main.java"], paths)
        self.assertNotEqual(result["head"], result["baseline_head"])
        self.assertTrue(all(change["status"].startswith("baseline:") for change in result["changes"]))


if __name__ == "__main__":
    unittest.main()
