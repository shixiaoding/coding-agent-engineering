#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "scripts" / "coding-rules-loop"
HOOK_ROOT = REPO_ROOT / "hooks" / "qoder"
sys.path.insert(0, str(CORE_ROOT))
sys.path.insert(0, str(HOOK_ROOT))

from core import RulesLoopCore  # noqa: E402
from coding_rules_loop_hook import QoderHookAdapter, discover_qoder_rules, extract_files  # noqa: E402


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repository), *arguments], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def percentile_95(values):
    return sorted(values)[max(0, int(len(values) * 0.95) - 1)]


class QoderHookAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repository = root / "repository"
        self.data_home = root / "data"
        self.repository.mkdir()
        git(self.repository, "init", "-q")
        git(self.repository, "config", "user.name", "fixture")
        git(self.repository, "config", "user.email", "fixture@example.com")
        self.source = self.repository / "src" / "main" / "java" / "example" / "OrderService.java"
        self.feign = self.repository / "src" / "main" / "java" / "example" / "OrderFeignClient.java"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("class OrderService {}\n")
        self.feign.write_text("interface OrderFeignClient {}\n")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-qm", "fixture")
        core = RulesLoopCore(REPO_ROOT / "rules", REPO_ROOT / "skills", self.data_home)
        self.adapter = QoderHookAdapter(core=core, data_home=self.data_home)
        self.base = {
            "session_id": "qoder-session-fixture",
            "cwd": str(self.repository),
            "transcript_path": str(self.repository / "transcript.jsonl"),
        }

    def tearDown(self):
        self.temporary.cleanup()

    def raw(self, event_name, **values):
        return {**self.base, "hook_event_name": event_name, **values}

    def test_non_git_hooks_are_silent_and_record_skip(self):
        self.base["cwd"] = self.temporary.name
        events = (
            self.raw("SessionStart", type="startup"),
            self.raw("UserPromptSubmit", prompt="修改 README.md"),
            self.raw("Stop", stop_hook_active=False),
        )
        for event in events:
            self.assertEqual({}, self.adapter.handle(event))
        records = [json.loads(line) for path in (self.data_home / "observations").glob("*.jsonl") for line in path.read_text().splitlines()]
        self.assertEqual(3, len(records))
        self.assertTrue(all(record["status"] == "skipped" for record in records))
        self.assertTrue(all(record["reason"] == "not_git_repository" for record in records))

    def test_plain_document_prompt_does_not_load_rules(self):
        for prompt in ("请修改 README.md", "修复文章中的错别字", "修改技术方案文档", "修改接口文档", "修改 Java 文档"):
            with self.subTest(prompt=prompt):
                self.assertEqual({}, self.adapter.handle(self.raw("UserPromptSubmit", prompt=prompt)))
                self.assertEqual({}, self.adapter.handle(self.raw("Stop", stop_hook_active=False)))
        self.assertEqual({}, self.adapter.handle(self.raw("UserPromptSubmit", prompt="修改 Java 代码并更新文档")))
        code_write = self.raw("PreToolUse", tool_name="Edit", tool_input={"file_path": "src/main/java/example/OrderService.java", "new_string": "value"})
        self.assertIn("hookSpecificOutput", self.adapter.handle(code_write))

    def test_every_supported_event_has_normal_abnormal_and_missing_fixture(self):
        fixture = json.loads((REPO_ROOT / "tests" / "fixtures" / "hooks" / "qoder" / "events.json").read_text())
        base = {**fixture["base"], "cwd": str(self.repository), "transcript_path": str(self.repository / "transcript.jsonl")}

        for case in fixture["events"]:
            with self.subTest(event=case["hook_event_name"], variant="normal"):
                normal = {**base, "hook_event_name": case["hook_event_name"], **case["normal"]}
                normalized = self.adapter.normalize(normal)
                self.assertIsNotNone(normalized)
                self.assertEqual(case["internal_event"], normalized["event"])

            with self.subTest(event=case["hook_event_name"], variant="abnormal"):
                abnormal = {**base, "hook_event_name": case["hook_event_name"], **case["abnormal"]}
                output = self.adapter.handle(abnormal)
                if case["hook_event_name"] in {"SessionStart", "UserPromptSubmit", "SubagentStart", "PreToolUse", "PostToolUse"}:
                    self.assertNotEqual({}, output)
                else:
                    self.assertEqual({}, output)

            with self.subTest(event=case["hook_event_name"], variant="missing"):
                missing = {**base, "hook_event_name": case["hook_event_name"], **case["normal"]}
                missing.pop(case["missing_field"], None)
                output = self.adapter.handle(missing)
                if case["hook_event_name"] in {"SessionStart", "UserPromptSubmit", "SubagentStart", "PreToolUse", "PostToolUse"}:
                    self.assertNotEqual({}, output)
                else:
                    self.assertEqual({}, output)

    def test_rules_context_is_delivered_once_for_qoder_edit(self):
        prompt = self.adapter.handle(self.raw("UserPromptSubmit", prompt="请修复订单查询问题"))
        tool_input = {"file_path": "src/main/java/example/OrderService.java", "new_string": "value"}
        first_write = self.adapter.handle(self.raw("PreToolUse", tool_name="Edit", tool_input=tool_input))
        repeated_write = self.adapter.handle(self.raw("PreToolUse", tool_name="Edit", tool_input=tool_input))

        self.assertIn("hookSpecificOutput", prompt)
        self.assertIn("hookSpecificOutput", first_write)
        self.assertEqual({}, repeated_write)

    def test_qoder_project_rules_participate_in_loaded_context(self):
        rule = self.repository / ".qoder" / "rules" / "team.md"
        rule.parent.mkdir(parents=True)
        rule.write_text("# Team Rule\n")
        normalized = self.adapter.normalize(self.raw("UserPromptSubmit", prompt="请修复订单查询问题"))
        output = self.adapter.handle(self.raw("UserPromptSubmit", prompt="请修复订单查询问题"))

        self.assertEqual([".qoder/rules/team.md"], discover_qoder_rules(self.repository))
        self.assertEqual([".qoder/rules/team.md"], normalized["project_rule_files"])
        self.assertIn(str(rule), output["hookSpecificOutput"]["additionalContext"])

    def test_qoder_file_tools_and_shell_tools_are_routed_without_read_false_positive(self):
        edit_event = self.raw(
            "PreToolUse",
            tool_name="search_replace",
            tool_input={"file_path": "src/main/java/example/OrderService.java", "new_string": "value"},
        )
        terminal_write = self.raw(
            "PreToolUse",
            tool_name="run_in_terminal",
            tool_input={"command": "tee src/main/java/example/OrderService.java"},
        )
        terminal_read = self.raw(
            "PreToolUse",
            tool_name="run_in_terminal",
            tool_input={"command": "git diff -- src/main/java/example/OrderService.java"},
        )

        self.assertEqual(["src/main/java/example/OrderService.java"], extract_files(edit_event))
        self.assertIsNotNone(self.adapter.normalize(edit_event))
        self.assertIsNotNone(self.adapter.normalize(terminal_write))
        self.assertIsNone(self.adapter.normalize(terminal_read))

    def test_new_controller_loads_api_reference_and_feign_keeps_compatibility_boundary(self):
        controller_path = "src/main/java/example/NewOrderController.java"
        controller_pre = self.adapter.handle(
            self.raw(
                "PreToolUse",
                tool_name="create_file",
                tool_input={"file_path": controller_path, "file_content": "class NewOrderController {}"},
            )
        )
        context = controller_pre["hookSpecificOutput"]["additionalContext"]
        self.assertIn("api-spec.md", context)

        self.adapter.handle(
            self.raw(
                "PreToolUse",
                tool_name="Edit",
                tool_input={"file_path": "src/main/java/example/OrderFeignClient.java", "new_string": "value"},
            )
        )
        self.feign.write_text("interface OrderFeignClient { int value = 1; }\n")
        self.adapter.handle(
            self.raw(
                "PostToolUse",
                tool_name="Edit",
                tool_input={"file_path": "src/main/java/example/OrderFeignClient.java", "new_string": "value"},
                tool_response="File written successfully",
            )
        )
        self.assertEqual({}, self.adapter.handle(self.raw("Stop", stop_hook_active=False, last_assistant_message="done")))

        records = []
        for path in (self.data_home / "observations").glob("*.jsonl"):
            records.extend(json.loads(line) for line in path.read_text().splitlines())
        stop_record = next(record for record in reversed(records) if record["event"] == "Stop")
        self.assertEqual("qoder-ide", stop_record["tool"])
        self.assertEqual("compatibility-only", stop_record["api_boundary"])

    def test_pre_compact_invalidates_context_until_next_write(self):
        tool_input = {"file_path": "src/main/java/example/OrderService.java", "new_string": "value"}
        self.adapter.handle(self.raw("PreToolUse", tool_name="Edit", tool_input=tool_input))
        compact_output = self.adapter.handle(self.raw("PreCompact", trigger="auto", custom_instructions=""))
        next_write = self.adapter.handle(self.raw("PreToolUse", tool_name="Edit", tool_input=tool_input))

        self.assertEqual({}, compact_output)
        self.assertIn("hookSpecificOutput", next_write)

    def test_configuration_targets_qoder_ide_command_hooks_only(self):
        configuration = json.loads((HOOK_ROOT / "coding-rules-loop.json").read_text())
        expected_events = {
            "SessionStart",
            "SessionEnd",
            "UserPromptSubmit",
            "SubagentStart",
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "Stop",
            "PreCompact",
        }
        self.assertEqual(expected_events, set(configuration["hooks"]))
        serialized = json.dumps(configuration, ensure_ascii=False)
        self.assertIn("CODING_RULES_LOOP_MODE=observe", serialized)
        self.assertIn("~/.qoder/skills/coding-rules-loop/scripts/qoder_hook.py", serialized)
        self.assertNotIn('"type": "prompt"', serialized)
        self.assertNotIn('"type": "agent"', serialized)
        self.assertNotIn('"decision": "block"', serialized)

    def test_skill_wrapper_runs_qoder_adapter_with_json_stdin_stdout(self):
        cli_home = Path(self.temporary.name) / "qoder-data"
        cli_home.mkdir()
        (cli_home / "rules").symlink_to(REPO_ROOT / "rules", target_is_directory=True)
        environment = {
            **os.environ,
            "CODING_RULES_LOOP_HOME": str(cli_home),
            "CODING_RULES_LOOP_SKILLS_ROOT": str(REPO_ROOT / "skills"),
            "CODING_RULES_LOOP_MODE": "observe",
        }
        process = subprocess.run(
            [sys.executable, str(REPO_ROOT / "skills" / "coding-rules-loop" / "scripts" / "qoder_hook.py")],
            input=json.dumps(self.raw("SessionStart", type="startup", model="Auto")).encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )

        self.assertEqual(0, process.returncode, process.stderr.decode())
        self.assertEqual({}, json.loads(process.stdout))

    def test_fixed_event_performance_is_ready_for_qoder_ide_smoke(self):
        fixtures = {
            "UserPromptSubmit": self.raw("UserPromptSubmit", prompt="请修复订单查询问题"),
            "PreToolUse": self.raw(
                "PreToolUse",
                tool_name="Edit",
                tool_input={"file_path": "src/main/java/example/OrderService.java", "new_string": "value"},
            ),
            "PostToolUse": self.raw(
                "PostToolUse",
                tool_name="Edit",
                tool_input={"file_path": "src/main/java/example/OrderService.java", "new_string": "value"},
                tool_response="File written successfully",
            ),
            "Stop": self.raw("Stop", stop_hook_active=False, last_assistant_message="done"),
        }
        report = {}
        for event_name, fixture in fixtures.items():
            durations = []
            for _ in range(100):
                started = time.perf_counter()
                self.adapter.handle(fixture)
                durations.append((time.perf_counter() - started) * 1000)
            report[event_name] = {
                "samples": len(durations),
                "average_ms": round(sum(durations) / len(durations), 3),
                "p95_ms": round(percentile_95(durations), 3),
                "max_ms": round(max(durations), 3),
            }

        print("QODER_HOOK_PERFORMANCE=" + json.dumps(report, sort_keys=True))
        for event_name in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
            self.assertLess(report[event_name]["p95_ms"], 100)
        self.assertLess(report["Stop"]["p95_ms"], 1000)


if __name__ == "__main__":
    unittest.main()
