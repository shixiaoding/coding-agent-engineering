#!/usr/bin/env python3

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "scripts" / "coding-rules-loop"
sys.path.insert(0, str(CORE_ROOT))

from core import RulesLoopCore  # noqa: E402
from workspace import WorkspaceError  # noqa: E402


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repository), *arguments], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class RulesLoopCoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repository = root / "repository"
        self.data_home = root / "data"
        self.repository.mkdir()
        git(self.repository, "init", "-q")
        git(self.repository, "config", "user.name", "fixture")
        git(self.repository, "config", "user.email", "fixture@example.com")
        source = self.repository / "src" / "main" / "java" / "example" / "OrderService.java"
        source.parent.mkdir(parents=True)
        source.write_text("class OrderService {}\n")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-qm", "fixture")
        self.source = source
        self.core = RulesLoopCore(REPO_ROOT / "rules", REPO_ROOT / "skills", self.data_home)
        self.base = {
            "repository_root": str(self.repository),
            "session_id": "session-1",
            "turn_id": "turn-1",
            "agent_id": "main",
            "tool": "fixture",
            "mode": "observe",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def event(self, event_name: str, **values):
        return self.core.process({**self.base, "event": event_name, **values})

    def test_non_coding_session_does_not_load_rules(self):
        initialized = self.event("session_initialized", reason="startup")
        intent = self.event("task_intent_received", coding_intent=False)

        self.assertFalse(initialized["load_required"])
        self.assertEqual([], initialized["assets_to_load"])
        self.assertFalse(intent["load_required"])

    def test_non_code_events_skip_even_when_rules_are_missing(self):
        self.core = RulesLoopCore(self.data_home / "missing-rules", self.data_home / "missing-skills", self.data_home)
        for name, values in (
            ("session_initialized", {"reason": "startup"}),
            ("task_intent_received", {"coding_intent": False}),
            ("before_task_finish", {}),
            ("before_code_write", {"files": ["docs/plan.md"]}),
        ):
            with self.subTest(event=name):
                result = self.event(name, **values)
                self.assertEqual("skipped", result["status"])
                self.assertFalse(result["load_required"])
                self.assertEqual([], result["semantic_checklist"])

    def test_non_git_workspace_skips_but_other_git_errors_remain_failures(self):
        result = self.core.process({**self.base, "repository_root": self.temporary.name, "event": "before_code_write", "files": ["A.java"]})
        self.assertEqual("skipped", result["status"])
        self.assertEqual("not_git_repository", result["reason"])
        self.assertFalse(self.data_home.exists())
        with patch("core.find_repository_root", side_effect=WorkspaceError("permission denied")):
            result = self.event("session_initialized")
        self.assertEqual("check_failed", result["status"])
        self.assertIn("permission denied", result["warnings"][0])

    def test_broken_git_marker_is_reported_as_failure(self):
        directory = Path(self.temporary.name) / "broken-repository"
        (directory / ".git").mkdir(parents=True)
        result = self.core.process({**self.base, "repository_root": str(directory), "event": "session_initialized"})
        self.assertEqual("check_failed", result["status"])

    def test_code_renamed_to_document_is_still_checked(self):
        self.event("task_intent_received", coding_intent=True)
        destination = self.repository / "OrderService.md"
        self.source.rename(destination)
        git(self.repository, "add", "-A")
        result = self.event("before_task_finish")
        self.assertEqual("check_required", result["status"])
        self.assertIn("coding-standards-java", result["required_skills"])
        self.assertIn("OrderService.md", result["changes"])

    def test_read_only_followup_skips_and_code_write_reactivates(self):
        self.event("before_code_write", files=["src/main/java/example/OrderService.java"])
        self.source.write_text("class OrderService { int first; }\n")
        self.event("after_code_write", files=["src/main/java/example/OrderService.java"])
        self.event("task_intent_received", turn_id="turn-2", coding_intent=False)
        with patch.object(self.core.selector, "select", side_effect=AssertionError("must not load rules")), patch("core.collect_changes", side_effect=AssertionError("must not check diff")):
            result = self.event("before_task_finish", turn_id="turn-2")
        self.assertEqual("skipped", result["status"])
        before = self.event("before_code_write", turn_id="turn-3", files=["src/main/java/example/OrderService.java"])
        self.assertNotEqual("skipped", before["status"])
        self.assertEqual("check_required", self.event("before_task_finish", turn_id="turn-3")["status"])

    def test_stop_ignores_document_changes_and_keeps_code_changes(self):
        self.event("task_intent_received", coding_intent=True)
        document = self.repository / "README.md"
        document.write_text("updated documentation\n")
        self.assertEqual("skipped", self.event("before_task_finish")["status"])
        self.source.write_text("class OrderService { int changed; }\n")
        result = self.event("before_task_finish")
        self.assertEqual("check_required", result["status"])
        self.assertEqual(["src/main/java/example/OrderService.java"], result["changes"])

    def test_session_mode_override_and_active_language_are_computed(self):
        overridden = self.event(
            "before_code_write",
            files=["src/main/java/example/OrderService.java"],
            mode_override="repair",
        )
        resumed = self.event("context_restored", reason="resume")

        self.assertEqual("repair", overridden["mode"])
        self.assertEqual("repair", resumed["mode"])
        self.assertEqual(["java"], resumed["active_languages"])
        self.assertTrue(resumed["load_required"])

    def test_invalid_json_shape_fails_open(self):
        result = self.core.process(["not-an-object"])

        self.assertEqual("check_failed", result["result"])
        self.assertEqual("core_failure", result["reason"])

    def test_loaded_skill_is_not_reloaded_and_diff_must_be_rechecked(self):
        before = self.event(
            "before_code_write",
            files=["src/main/java/example/OrderService.java"],
        )
        self.assertTrue(before["load_required"])
        self.assertEqual(["coding-standards-java"], before["required_skills"])

        self.source.write_text("class OrderService { int value; }\n")
        after = self.event(
            "after_code_write",
            files=["src/main/java/example/OrderService.java"],
            loaded_rules_hash=before["loaded_rules_hash"],
        )
        self.assertFalse(after["load_required"])

        first_finish = self.event("before_task_finish")
        self.assertEqual("check_required", first_finish["status"])
        self.assertEqual("pending", first_finish["check"]["tool_status"])
        self.assertEqual("pending", first_finish["check"]["semantic_status"])
        self.assertEqual(1, first_finish["check"]["continuation_count"])
        self.assertTrue(first_finish["semantic_checklist"])

        passed = self.event(
            "before_task_finish",
            check_result={
                "diff_hash": first_finish["current_diff_hash"],
                "tool_status": "passed",
                "semantic_status": "passed",
                "findings": [],
            },
        )
        self.assertEqual("passed", passed["result"])
        self.assertEqual(0, passed["check"]["continuation_count"])
        self.assertEqual("passed", self.event("before_task_finish")["result"])

        self.source.write_text("class OrderService { int value; int next; }\n")
        self.event("after_code_write", files=["src/main/java/example/OrderService.java"])
        changed = self.event("before_task_finish")
        self.assertEqual("check_required", changed["status"])
        self.assertNotEqual(first_finish["current_diff_hash"], changed["current_diff_hash"])

    def test_stale_check_result_restarts_check_for_current_diff(self):
        self.event("before_code_write", files=["src/main/java/example/OrderService.java"])
        self.source.write_text("class OrderService { int first; }\n")
        self.event("after_code_write", files=["src/main/java/example/OrderService.java"])
        first = self.event("before_task_finish")
        self.source.write_text("class OrderService { int second; }\n")
        self.event("after_code_write", files=["src/main/java/example/OrderService.java"])

        restarted = self.event(
            "before_task_finish",
            check_result={
                "diff_hash": first["current_diff_hash"],
                "tool_status": "passed",
                "semantic_status": "passed",
                "findings": [],
            },
        )

        self.assertEqual("check_required", restarted["status"])
        self.assertEqual("stale_check_result", restarted["reason"])
        self.assertNotEqual(first["current_diff_hash"], restarted["current_diff_hash"])
        self.assertEqual("pending", restarted["check"]["tool_status"])

    def test_same_diff_check_continuation_is_limited(self):
        self.event("before_code_write", files=["src/main/java/example/OrderService.java"])
        self.source.write_text("class OrderService { int value; }\n")
        self.event("after_code_write", files=["src/main/java/example/OrderService.java"])

        for expected_count in range(1, 4):
            pending = self.event("before_task_finish")
            self.assertEqual("check_required", pending["status"])
            self.assertEqual(expected_count, pending["check"]["continuation_count"])
        limited = self.event("before_task_finish")

        self.assertEqual("check_failed", limited["status"])
        self.assertEqual("check_continuation_limit", limited["reason"])
        self.assertIn("连续三次", limited["warnings"][0])

    def test_incomplete_check_result_is_rejected_and_retried(self):
        self.event("before_code_write", files=["src/main/java/example/OrderService.java"])
        self.source.write_text("class OrderService { int value; }\n")
        self.event("after_code_write", files=["src/main/java/example/OrderService.java"])
        pending = self.event("before_task_finish")

        retried = self.event(
            "before_task_finish",
            check_result={
                "diff_hash": pending["current_diff_hash"],
                "semantic_status": "passed",
                "findings": [],
            },
        )

        self.assertEqual("check_required", retried["status"])
        self.assertEqual("invalid_check_result", retried["reason"])
        self.assertEqual(2, retried["check"]["continuation_count"])

    def test_compact_context_forces_reload(self):
        before = self.event("before_code_write", files=["src/main/java/example/OrderService.java"])
        self.event(
            "after_code_write",
            files=["src/main/java/example/OrderService.java"],
            loaded_rules_hash=before["loaded_rules_hash"],
        )
        restored = self.event("context_restored", reason="compact")

        self.assertTrue(restored["load_required"])
        self.assertTrue(restored["assets_to_load"])

    def test_pre_compact_without_context_output_invalidates_next_load(self):
        before = self.event("before_code_write", files=["src/main/java/example/OrderService.java"])
        self.event(
            "after_code_write",
            files=["src/main/java/example/OrderService.java"],
            loaded_rules_hash=before["loaded_rules_hash"],
        )
        compacting = self.event(
            "context_restored",
            reason="compact",
            invalidate_rules_context=True,
        )
        next_write = self.event("before_code_write", files=["src/main/java/example/OrderService.java"])

        self.assertTrue(compacting["load_required"])
        self.assertTrue(next_write["load_required"])

    def test_new_agent_loads_required_skills_independently(self):
        before = self.event("before_code_write", files=["src/main/java/example/OrderService.java"])
        self.event(
            "after_code_write",
            files=["src/main/java/example/OrderService.java"],
            loaded_rules_hash=before["loaded_rules_hash"],
        )

        child = self.core.process(
            {
                **self.base,
                "event": "agent_started",
                "agent_id": "child-1",
                "coding_agent": True,
                "files": ["src/main/java/example/OrderService.java"],
            }
        )

        self.assertTrue(child["load_required"])
        self.assertIn("coding-standards-java", child["required_skills"])

    def test_observe_reports_repair_without_allowing_automatic_repair(self):
        self.event("before_code_write", files=["src/main/java/example/OrderService.java"])
        self.source.write_text("class OrderService { int value; }\n")
        self.event("after_code_write", files=["src/main/java/example/OrderService.java"])
        pending = self.event("before_task_finish")
        result = self.event(
            "before_task_finish",
            check_result={
                "diff_hash": pending["current_diff_hash"],
                "tool_status": "passed",
                "semantic_status": "failed",
                "findings": [
                    {
                        "result": "repairable",
                        "rule_id": "coding.comments-explain-why",
                        "issue_type": "comment",
                        "file": "OrderService.java",
                        "suggestion": "补充业务原因",
                    }
                ],
            },
        )

        self.assertEqual("repairable", result["result"])
        self.assertTrue(result["would_repair"])
        self.assertFalse(result["repair_allowed"])
        self.assertEqual("补充业务原因", result["repair_suggestions"][0]["action"])

    def test_ambiguous_finding_returns_user_question_without_persisting_text(self):
        self.event("before_code_write", files=["src/main/java/example/OrderService.java"])
        self.source.write_text("class OrderService { int value; }\n")
        self.event("after_code_write", files=["src/main/java/example/OrderService.java"])
        pending = self.event("before_task_finish")
        result = self.event(
            "before_task_finish",
            check_result={
                "diff_hash": pending["current_diff_hash"],
                "tool_status": "passed",
                "semantic_status": "failed",
                "findings": [
                    {
                        "result": "needs_user_decision",
                        "rule_id": "api.preserve-published-contract",
                        "issue_type": "compatibility",
                        "file": "OrderService.java",
                        "question": "是否允许修改已发布契约？",
                    }
                ],
            },
        )

        self.assertEqual("needs_user_decision", result["result"])
        self.assertEqual("是否允许修改已发布契约？", result["user_questions"][0]["question"])
        state_text = self.core.store.state_path(str(self.repository), "session-1").read_text()
        self.assertNotIn("是否允许修改已发布契约", state_text)

    def test_missing_global_rules_fails_open(self):
        broken = RulesLoopCore(self.data_home / "missing-rules", REPO_ROOT / "skills", self.data_home)
        result = broken.process({**self.base, "event": "before_code_write", "files": ["Main.java"]})

        self.assertEqual("check_failed", result["result"])
        self.assertEqual("core_failure", result["reason"])

    def test_stop_uses_actual_workspace_changes_when_tool_reports_no_path(self):
        self.event("before_code_write", files=[])
        self.source.write_text("class OrderService { int generated; }\n")
        self.event("after_code_write", files=[])

        result = self.event("before_task_finish")

        self.assertEqual("check_required", result["status"])
        self.assertEqual(["coding-standards-java"], result["required_skills"])
        self.assertIn("src/main/java/example/OrderService.java", result["changes"])


if __name__ == "__main__":
    unittest.main()
