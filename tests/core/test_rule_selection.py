#!/usr/bin/env python3

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "scripts" / "coding-rules-loop"
sys.path.insert(0, str(CORE_ROOT))

from core import CoreError, RuleSelector  # noqa: E402


class RuleSelectorTest(unittest.TestCase):
    def setUp(self):
        self.selector = RuleSelector(REPO_ROOT / "rules", REPO_ROOT / "skills")

    def test_selection_cases(self):
        cases = json.loads((REPO_ROOT / "tests" / "fixtures" / "rules" / "selection-cases.json").read_text())
        for case in cases:
            with self.subTest(case=case["name"]):
                selection = self.selector.select(
                    REPO_ROOT,
                    case["files"],
                    change_hints=case["change_hints"],
                )
                self.assertEqual(case["skills"], selection["skills"])
                self.assertEqual(case["api_boundary"], selection["api_boundary"])
                self.assertEqual(
                    case["api_reference"],
                    "api-design/references/api-spec.md" in selection["references"],
                )
                self.assertEqual(
                    case["java_reference"],
                    "coding-standards-java/references/java-spec.md" in selection["references"],
                )

    def test_reference_change_invalidates_rules_without_forcing_full_reference_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(REPO_ROOT / "rules", root / "rules")
            shutil.copytree(REPO_ROOT / "skills", root / "skills")
            selector = RuleSelector(root / "rules", root / "skills")
            before = selector.select(REPO_ROOT, ["src/main/java/com/example/OrderService.java"])

            reference = root / "skills" / "coding-standards-java" / "references" / "java-spec.md"
            reference.write_text(reference.read_text() + "\n补充规范。\n")
            after = selector.select(REPO_ROOT, ["src/main/java/com/example/OrderService.java"])

            self.assertNotEqual(before["rules_hash"], after["rules_hash"])
            self.assertEqual(before["loaded_rules_hash"], after["loaded_rules_hash"])
            self.assertEqual([], after["references"])

    def test_requested_skill_must_be_enabled_by_common_rules(self):
        with self.assertRaises(CoreError):
            self.selector.select(REPO_ROOT, ["README.md"], requested_skills=["unknown-skill"])

    def test_project_rule_is_part_of_selection_hash(self):
        first = self.selector.select(REPO_ROOT, ["README.md"])
        self.assertTrue(any(path.endswith("AGENTS.md") for path in first["project_rules"]))

    def test_multi_file_api_keeps_feign_compatibility_boundary(self):
        selection = self.selector.select(
            REPO_ROOT,
            [
                "src/main/java/example/OrderController.java",
                "src/main/java/example/OrderFeignClient.java",
            ],
        )

        self.assertEqual("full", selection["api_file_boundaries"]["src/main/java/example/OrderController.java"])
        self.assertEqual(
            "compatibility-only",
            selection["api_file_boundaries"]["src/main/java/example/OrderFeignClient.java"],
        )


if __name__ == "__main__":
    unittest.main()
