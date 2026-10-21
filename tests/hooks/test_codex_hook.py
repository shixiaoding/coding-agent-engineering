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
HOOK_ROOT = REPO_ROOT / "hooks" / "codex"
sys.path.insert(0, str(CORE_ROOT))
sys.path.insert(0, str(HOOK_ROOT))

from core import RulesLoopCore  # noqa: E402
from coding_rules_loop_hook import (  # noqa: E402
    CodexHookAdapter,
    classify_coding_intent,
    classify_user_decision,
    extract_check_result,
    extract_files,
)


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repository), *arguments], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def percentile_95(values):
    return sorted(values)[max(0, int(len(values) * 0.95) - 1)]


class CodexHookAdapterTest(unittest.TestCase):
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
        core = RulesLoopCore(REPO_ROOT / "rules", REPO_ROOT / "skills", self.data_home)
        self.adapter = CodexHookAdapter(core=core, data_home=self.data_home, legacy_hook_active=True)
        self.base = {
            "session_id": "session-fixture",
            "turn_id": "turn-fixture",
            "cwd": str(self.repository),
            "permission_mode": "default",
            "model": "fixture-model",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def raw(self, event_name, **values):
        return {**self.base, "hook_event_name": event_name, **values}

    def test_non_git_hooks_are_silent_and_record_skip(self):
        self.base["cwd"] = self.temporary.name
        events = (
            self.raw("SessionStart", source="startup"),
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
        code_write = self.raw("PreToolUse", tool_name="apply_patch", tool_input={"command": "*** Begin Patch\n*** Update File: src/main/java/example/OrderService.java\n*** End Patch"})
        self.assertIn("hookSpecificOutput", self.adapter.handle(code_write))

    def test_every_supported_event_has_normal_abnormal_and_missing_fixture(self):
        fixture = json.loads((REPO_ROOT / "tests" / "fixtures" / "hooks" / "codex" / "events.json").read_text())
        base = {**fixture["base"], "cwd": str(self.repository)}

        for case in fixture["events"]:
            with self.subTest(event=case["hook_event_name"], variant="normal"):
                normal = {**base, "hook_event_name": case["hook_event_name"], **case["normal"]}
                normalized = self.adapter.normalize(normal)
                self.assertIsNotNone(normalized)
                self.assertEqual(case["internal_event"], normalized["event"])

            with self.subTest(event=case["hook_event_name"], variant="abnormal"):
                abnormal = {**base, "hook_event_name": case["hook_event_name"], **case["abnormal"]}
                self.assertIn("systemMessage", self.adapter.handle(abnormal))

            with self.subTest(event=case["hook_event_name"], variant="missing"):
                missing = {**base, "hook_event_name": case["hook_event_name"], **case["normal"]}
                missing.pop(case["missing_field"], None)
                self.assertIn("systemMessage", self.adapter.handle(missing))

    def test_rules_context_is_delivered_once_per_continuous_agent_context(self):
        prompt = self.adapter.handle(self.raw("UserPromptSubmit", prompt="请修复订单查询问题"))
        patch_input = {
            "command": "*** Begin Patch\n*** Update File: src/main/java/example/OrderService.java\n*** End Patch"
        }
        first_write = self.adapter.handle(
            self.raw("PreToolUse", tool_name="apply_patch", tool_use_id="tool-1", tool_input=patch_input)
        )
        repeated_write = self.adapter.handle(
            self.raw("PreToolUse", tool_name="apply_patch", tool_use_id="tool-2", tool_input=patch_input)
        )

        self.assertIn("hookSpecificOutput", prompt)
        self.assertIn("hookSpecificOutput", first_write)
        self.assertEqual({}, repeated_write)

    def test_resume_and_compact_map_to_context_restore_without_post_compact(self):
        for source in ("resume", "compact"):
            with self.subTest(source=source):
                normalized = self.adapter.normalize(self.raw("SessionStart", source=source))
                self.assertEqual("context_restored", normalized["event"])
                self.assertEqual(source, normalized["reason"])

        configuration = json.loads((HOOK_ROOT / "coding-rules-loop.json").read_text())
        self.assertNotIn("PostCompact", configuration["hooks"])

    def test_read_only_subagent_does_not_receive_coding_rules(self):
        self.adapter.handle(self.raw("UserPromptSubmit", prompt="请修复订单查询问题"))
        output = self.adapter.handle(
            self.raw("SubagentStart", agent_id="child-explorer", agent_type="explorer")
        )

        self.assertEqual({}, output)

    def test_post_write_and_stop_requests_semantic_check(self):
        patch_input = {
            "command": "*** Begin Patch\n*** Update File: src/main/java/example/OrderService.java\n*** End Patch"
        }
        self.adapter.handle(
            self.raw("PreToolUse", tool_name="apply_patch", tool_use_id="tool-1", tool_input=patch_input)
        )
        self.source.write_text("class OrderService { int value; }\n")
        self.adapter.handle(
            self.raw(
                "PostToolUse",
                tool_name="apply_patch",
                tool_use_id="tool-1",
                tool_input=patch_input,
                tool_response={"status": "ok"},
            )
        )
        stop_output = self.adapter.handle(
            self.raw("Stop", stop_hook_active=False, last_assistant_message="done")
        )

        self.assertEqual("block", stop_output["decision"])
        self.assertIn("当前 diff 指纹", stop_output["reason"])
        self.assertIn("coding-rules-loop-result", stop_output["reason"])
        self.assertIn('"result":"repairable"', stop_output["reason"])
        records = []
        for path in (self.data_home / "observations").glob("*.jsonl"):
            records.extend(json.loads(line) for line in path.read_text().splitlines())
        stop_record = next(record for record in reversed(records) if record["event"] == "Stop")
        self.assertEqual("check_required", stop_record["status"])
        self.assertTrue(stop_record["legacy_hook_active"])
        self.assertGreater(stop_record["semantic_rule_count"], 0)
        self.assertEqual("pending", stop_record["tool_status"])
        self.assertEqual("pending", stop_record["semantic_status"])
        self.assertEqual(1, stop_record["continuation_count"])
        self.assertNotIn("prompt", stop_record)
        self.assertNotIn("diff", stop_record)

    def test_stop_parses_hidden_check_result_and_completes_same_diff(self):
        patch_input = {
            "command": "*** Begin Patch\n*** Update File: src/main/java/example/OrderService.java\n*** End Patch"
        }
        self.adapter.handle(
            self.raw("PreToolUse", tool_name="apply_patch", tool_use_id="tool-1", tool_input=patch_input)
        )
        self.source.write_text("class OrderService { int value; }\n")
        self.adapter.handle(
            self.raw(
                "PostToolUse",
                tool_name="apply_patch",
                tool_use_id="tool-1",
                tool_input=patch_input,
                tool_response={"status": "ok"},
            )
        )
        self.adapter.handle(self.raw("Stop", stop_hook_active=False, last_assistant_message="done"))
        state, _ = self.adapter.core.store.load(str(self.repository), "session-fixture")
        payload = {
            "diff_hash": state["check"]["diff_hash"],
            "tool_status": "passed",
            "semantic_status": "passed",
            "findings": [],
        }
        message = "已完成检查。\n<!-- coding-rules-loop-result\n" + json.dumps(payload) + "\n-->"

        output = self.adapter.handle(
            self.raw("Stop", stop_hook_active=True, last_assistant_message=message)
        )
        completed, _ = self.adapter.core.store.load(str(self.repository), "session-fixture")

        self.assertEqual({}, output)
        self.assertEqual("passed", completed["check"]["tool_status"])
        self.assertEqual("passed", completed["check"]["semantic_status"])
        self.assertEqual(0, completed["check"]["continuation_count"])
        self.assertEqual(payload, extract_check_result(message))

    def test_stale_check_result_requests_new_diff_check(self):
        patch_input = {
            "command": "*** Begin Patch\n*** Update File: src/main/java/example/OrderService.java\n*** End Patch"
        }
        self.adapter.handle(
            self.raw("PreToolUse", tool_name="apply_patch", tool_use_id="tool-1", tool_input=patch_input)
        )
        self.source.write_text("class OrderService { int value; }\n")
        self.adapter.handle(
            self.raw("PostToolUse", tool_name="apply_patch", tool_use_id="tool-1", tool_input=patch_input, tool_response={})
        )
        self.adapter.handle(self.raw("Stop", stop_hook_active=False, last_assistant_message="done"))
        state, _ = self.adapter.core.store.load(str(self.repository), "session-fixture")
        old_hash = state["check"]["diff_hash"]
        self.source.write_text("class OrderService { int value; int next; }\n")
        self.adapter.handle(
            self.raw("PostToolUse", tool_name="apply_patch", tool_use_id="tool-2", tool_input=patch_input, tool_response={})
        )
        stale = {
            "diff_hash": old_hash,
            "tool_status": "passed",
            "semantic_status": "passed",
            "findings": [],
        }
        message = "<!-- coding-rules-loop-result\n" + json.dumps(stale) + "\n-->"

        output = self.adapter.handle(
            self.raw("Stop", stop_hook_active=True, last_assistant_message=message)
        )

        self.assertEqual("block", output["decision"])
        self.assertIn("旧 diff", output["reason"])
        self.assertNotIn(old_hash, output["reason"].split("当前 diff 指纹：", 1)[1].splitlines()[0])

    def test_repair_mode_continues_for_repairable_findings(self):
        self.adapter.mode = "repair"
        patch_input = {
            "command": "*** Begin Patch\n*** Update File: src/main/java/example/OrderService.java\n*** End Patch"
        }
        self.adapter.handle(
            self.raw("PreToolUse", tool_name="apply_patch", tool_use_id="tool-1", tool_input=patch_input)
        )
        self.source.write_text("class OrderService { int value; }\n")
        self.adapter.handle(
            self.raw("PostToolUse", tool_name="apply_patch", tool_use_id="tool-1", tool_input=patch_input, tool_response={})
        )
        check_request = self.adapter.handle(
            self.raw("Stop", stop_hook_active=False, last_assistant_message="done")
        )
        self.assertIn("先用 repairable findings 回写", check_request["reason"])
        state, _ = self.adapter.core.store.load(str(self.repository), "session-fixture")
        finding = {
            "diff_hash": state["check"]["diff_hash"],
            "tool_status": "passed",
            "semantic_status": "failed",
            "findings": [
                {
                    "result": "repairable",
                    "rule_id": "coding.comments-explain-why",
                    "issue_type": "comment",
                    "file": "OrderService.java",
                    "suggestion": "删除无价值注释",
                }
            ],
        }
        message = "<!-- coding-rules-loop-result\n" + json.dumps(finding) + "\n-->"

        output = self.adapter.handle(
            self.raw("Stop", stop_hook_active=True, last_assistant_message=message)
        )

        self.assertEqual("block", output["decision"])
        self.assertIn("删除无价值注释", output["reason"])
        repaired_state, _ = self.adapter.core.store.load(str(self.repository), "session-fixture")
        self.assertEqual(1, repaired_state["repair"]["round"])

    def test_invalid_result_and_continuation_limit_fail_open_with_warning(self):
        patch_input = {
            "command": "*** Begin Patch\n*** Update File: src/main/java/example/OrderService.java\n*** End Patch"
        }
        self.adapter.handle(
            self.raw("PreToolUse", tool_name="apply_patch", tool_use_id="tool-1", tool_input=patch_input)
        )
        self.source.write_text("class OrderService { int value; }\n")
        self.adapter.handle(
            self.raw("PostToolUse", tool_name="apply_patch", tool_use_id="tool-1", tool_input=patch_input, tool_response={})
        )

        invalid = self.adapter.handle(
            self.raw(
                "Stop",
                stop_hook_active=True,
                last_assistant_message="<!-- coding-rules-loop-result\n{broken}\n-->",
            )
        )
        self.assertEqual("block", invalid["decision"])
        self.assertIn("结构化检查结果无效", invalid["reason"])

        for _ in range(2):
            output = self.adapter.handle(
                self.raw("Stop", stop_hook_active=True, last_assistant_message="done")
            )
            self.assertEqual("block", output["decision"])
        limited = self.adapter.handle(
            self.raw("Stop", stop_hook_active=True, last_assistant_message="done")
        )
        self.assertIn("systemMessage", limited)
        self.assertIn("连续三次", limited["systemMessage"])

    def test_read_only_prompt_and_document_patch_do_not_activate_coding(self):
        self.assertFalse(classify_coding_intent("请分析这个修改是否合理，不要实现"))
        self.assertTrue(classify_coding_intent("请先分析调用链并修改这个问题"))
        self.assertTrue(classify_user_decision("好的，可以按这个方案处理"))
        self.assertTrue(classify_user_decision("可以"))
        self.assertFalse(classify_user_decision("现在是否可以执行？"))
        self.assertFalse(classify_user_decision("another analysis request"))
        document_patch = self.raw(
            "PreToolUse",
            tool_name="apply_patch",
            tool_use_id="tool-doc",
            tool_input={"command": "*** Begin Patch\n*** Update File: docs/plan.md\n*** End Patch"},
        )

        self.assertEqual([], extract_files(document_patch))
        self.assertIsNone(self.adapter.normalize(document_patch))

    def test_bash_and_mcp_write_detection_uses_reported_paths(self):
        bash_write = self.raw(
            "PreToolUse",
            tool_name="Bash",
            tool_use_id="tool-bash",
            tool_input={"command": "tee src/main/java/example/OrderService.java"},
        )
        bash_read = self.raw(
            "PreToolUse",
            tool_name="Bash",
            tool_use_id="tool-read",
            tool_input={"command": "git diff -- src/main/java/example/OrderService.java"},
        )
        mcp_write = self.raw(
            "PreToolUse",
            tool_name="mcp__filesystem__write_file",
            tool_use_id="tool-mcp",
            tool_input={"path": "src/main/java/example/OrderService.java", "content": "class OrderService {}"},
        )

        self.assertEqual(["src/main/java/example/OrderService.java"], extract_files(bash_write))
        self.assertIsNotNone(self.adapter.normalize(bash_write))
        self.assertIsNone(self.adapter.normalize(bash_read))
        self.assertIsNotNone(self.adapter.normalize(mcp_write))

        directory_write = self.raw(
            "PreToolUse",
            tool_name="Bash",
            tool_use_id="tool-directory",
            tool_input={"command": "mkdir docs/generated"},
        )
        formatter_write = self.raw(
            "PreToolUse",
            tool_name="Bash",
            tool_use_id="tool-formatter",
            tool_input={"command": "mvn spotless:apply"},
        )
        self.assertIsNone(self.adapter.normalize(directory_write))
        self.assertIsNotNone(self.adapter.normalize(formatter_write))

    def test_hook_configuration_keeps_observe_during_controlled_rollout(self):
        configuration = json.loads((HOOK_ROOT / "coding-rules-loop.json").read_text())

        self.assertEqual(
            {"SessionStart", "SessionEnd", "UserPromptSubmit", "SubagentStart", "PreToolUse", "PostToolUse", "Stop", "Interrupt"},
            set(configuration["hooks"]),
        )
        serialized = json.dumps(configuration, ensure_ascii=False)
        self.assertIn("CODING_RULES_LOOP_MODE=observe", serialized)
        self.assertIn("CODING_RULES_LOOP_LEGACY_HOOK_ACTIVE=0", serialized)
        self.assertIn("coding-rules-loop/scripts/codex_hook.py", serialized)
        self.assertEqual(3, configuration["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"])
        self.assertEqual(3, configuration["hooks"]["Interrupt"][0]["hooks"][0]["timeout"])

    def test_skill_wrapper_runs_adapter_with_json_stdin_stdout(self):
        cli_home = Path(self.temporary.name) / "cli-data"
        cli_home.mkdir()
        (cli_home / "rules").symlink_to(REPO_ROOT / "rules", target_is_directory=True)
        hook_input = self.raw("SessionStart", source="startup")
        environment = {
            **os.environ,
            "CODING_RULES_LOOP_HOME": str(cli_home),
            "CODING_RULES_LOOP_SKILLS_ROOT": str(REPO_ROOT / "skills"),
            "CODING_RULES_LOOP_MODE": "observe",
        }

        process = subprocess.run(
            [sys.executable, str(REPO_ROOT / "skills" / "coding-rules-loop" / "scripts" / "codex_hook.py")],
            input=json.dumps(hook_input).encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )

        self.assertEqual(0, process.returncode, process.stderr.decode())
        self.assertEqual({}, json.loads(process.stdout))

    def test_fixed_event_performance_targets(self):
        fixtures = {
            "UserPromptSubmit": self.raw("UserPromptSubmit", prompt="请修复订单查询问题"),
            "PreToolUse": self.raw(
                "PreToolUse",
                tool_name="apply_patch",
                tool_use_id="tool-perf",
                tool_input={"command": "*** Begin Patch\n*** Update File: src/main/java/example/OrderService.java\n*** End Patch"},
            ),
            "PostToolUse": self.raw(
                "PostToolUse",
                tool_name="apply_patch",
                tool_use_id="tool-perf",
                tool_input={"command": "*** Begin Patch\n*** Update File: src/main/java/example/OrderService.java\n*** End Patch"},
                tool_response={"status": "ok"},
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

        print("CODEX_HOOK_PERFORMANCE=" + json.dumps(report, sort_keys=True))
        for event_name in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
            self.assertLess(report[event_name]["p95_ms"], 100)
        self.assertLess(report["Stop"]["p95_ms"], 1000)


if __name__ == "__main__":
    unittest.main()
