#!/usr/bin/env python3

import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "scripts" / "coding-rules-loop"
sys.path.insert(0, str(CORE_ROOT))

from state import StateStore, evaluate_findings, new_state, transition_state  # noqa: E402


def repairable(rule_id: str):
    return [{"result": "repairable", "rule_id": rule_id, "issue_type": "style", "file": "Main.java"}]


class StateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_home = Path(self.temporary.name)
        self.store = StateStore(self.data_home)
        self.repository = str(REPO_ROOT)
        self.session = "session-1"

    def tearDown(self):
        self.temporary.cleanup()

    def test_state_is_written_atomically_and_loaded(self):
        state, warnings = self.store.update(
            self.repository,
            self.session,
            lambda current: transition_state(
                current,
                "after_code_write",
                {"turn_id": "turn-1", "agent_id": "agent-1", "files": ["A.java"], "active_skills": ["java"]},
            ),
        )
        loaded, load_warnings = self.store.load(self.repository, self.session)

        self.assertEqual([], warnings)
        self.assertEqual([], load_warnings)
        self.assertEqual(["A.java"], state["touched_files"])
        self.assertEqual(state, loaded)
        self.assertEqual(0o600, self.store.state_path(self.repository, self.session).stat().st_mode & 0o777)

    def test_concurrent_agent_updates_merge_without_overwrite(self):
        def update(index: int):
            self.store.update(
                self.repository,
                self.session,
                lambda current: transition_state(
                    current,
                    "after_code_write",
                    {
                        "turn_id": "turn-1",
                        "agent_id": f"agent-{index}",
                        "files": [f"File{index}.java"],
                        "active_skills": ["coding-standards-java"],
                    },
                ),
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(update, range(8)))
        state, _ = self.store.load(self.repository, self.session)

        self.assertEqual(8, len(state["touched_files"]))
        self.assertEqual(8, len(state["agents"]))

    def test_corrupt_state_is_isolated_and_fails_open(self):
        path = self.store.state_path(self.repository, self.session)
        path.parent.mkdir(parents=True)
        path.write_text("{broken")

        state, warnings = self.store.load(self.repository, self.session)

        self.assertEqual(1, state["schema_version"])
        self.assertEqual(1, len(warnings))
        self.assertTrue(list(path.parent.glob("*.corrupt-*.json")))

    def test_inactive_state_cleanup(self):
        self.store.update(self.repository, self.session, lambda state: state)
        path = self.store.state_path(self.repository, self.session)
        old = (datetime.now(timezone.utc) - timedelta(days=31)).timestamp()
        os.utime(path, (old, old))

        self.assertEqual(1, self.store.cleanup())
        self.assertFalse(path.exists())

    def test_repair_limit_and_loop_protection(self):
        state = new_state(self.repository, self.session)
        for index in range(3):
            decision = evaluate_findings(state, repairable(f"rule-{index}"), f"diff-{index}", "repair")
            self.assertTrue(decision["repair_allowed"])
        decision = evaluate_findings(state, repairable("rule-3"), "diff-3", "repair")
        self.assertEqual("repair_limit_reached", decision["reason"])

        unchanged = new_state(self.repository, "unchanged")
        evaluate_findings(unchanged, repairable("rule-a"), "diff-a", "repair")
        decision = evaluate_findings(unchanged, repairable("rule-b"), "diff-a", "repair")
        self.assertEqual("diff_unchanged_after_repair", decision["reason"])

        repeated = new_state(self.repository, "repeated")
        evaluate_findings(repeated, repairable("rule-a"), "diff-a", "repair")
        decision = evaluate_findings(repeated, repairable("rule-a"), "diff-b", "repair")
        self.assertEqual("same_issue_repeated", decision["reason"])

        cycle = new_state(self.repository, "cycle")
        evaluate_findings(cycle, repairable("rule-a"), "diff-a", "repair")
        evaluate_findings(cycle, repairable("rule-b"), "diff-b", "repair")
        decision = evaluate_findings(cycle, repairable("rule-c"), "diff-a", "repair")
        self.assertEqual("diff_cycle", decision["reason"])

    def test_user_decision_starts_a_new_repair_chain(self):
        state = new_state(self.repository, self.session)
        state["repair"]["waiting_for_user"] = True
        state["repair"]["pending_issue_hash"] = "issue"
        state["repair"]["round"] = 2

        updated = transition_state(
            state,
            "task_intent_received",
            {"turn_id": "turn-2", "user_decision_received": True},
        )

        self.assertFalse(updated["repair"]["waiting_for_user"])
        self.assertEqual(0, updated["repair"]["round"])
        self.assertEqual("not_started", updated["check"]["semantic_status"])

    def test_lifecycle_transitions_preserve_session_and_clear_turn_prewrite(self):
        state = new_state(self.repository, self.session)
        state = transition_state(state, "session_initialized", {"turn_id": "turn-1"})
        state = transition_state(
            state,
            "task_intent_received",
            {"turn_id": "turn-1", "coding_intent": True, "baseline": {"head": "abc"}},
        )
        state = transition_state(state, "before_code_write", {"turn_id": "turn-1"})
        interrupted = transition_state(state, "turn_interrupted", {"turn_id": "turn-1"})
        ended = transition_state(interrupted, "session_ended", {"turn_id": "turn-1"})

        self.assertTrue(ended["coding_active"])
        self.assertEqual({"head": "abc"}, ended["baseline"])
        self.assertFalse(ended["current_turn"]["prewrite_active"])

        self.store.update(self.repository, self.session, lambda _: ended)
        self.store.clear(self.repository, self.session)
        cleared, _ = self.store.load(self.repository, self.session)
        self.assertFalse(cleared["coding_active"])


if __name__ == "__main__":
    unittest.main()
