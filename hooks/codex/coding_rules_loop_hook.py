#!/usr/bin/env python3
"""将 Codex Hook 输入转换为 Coding Rules Loop 统一事件。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set


SOURCE_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = SOURCE_ROOT / "scripts" / "coding-rules-loop"
sys.path.insert(0, str(CORE_ROOT))
sys.path.insert(0, str(SOURCE_ROOT / "hooks"))

from adapter_common import (  # noqa: E402
    classify_change_hints,
    classify_coding_intent,
    classify_user_decision,
    command_file_values,
    is_write_command,
    normalize_path,
    path_values,
)
from core import RulesLoopCore  # noqa: E402
from workspace import WorkspaceError, find_repository_root  # noqa: E402


EVENT_MAP = {
    "SessionEnd": "session_ended",
    "UserPromptSubmit": "task_intent_received",
    "SubagentStart": "agent_started",
    "PreToolUse": "before_code_write",
    "PostToolUse": "after_code_write",
    "Stop": "before_task_finish",
    "Interrupt": "turn_interrupted",
}
PATCH_FILE_PATTERN = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
PATCH_MOVE_PATTERN = re.compile(r"^\*\*\* Move to: (.+)$", re.MULTILINE)
WRITE_TOOL_PATTERN = re.compile(r"(?:apply_patch|edit|write|create|delete|move|rename|format)", re.IGNORECASE)
CHECK_RESULT_PATTERN = re.compile(r"<!--\s*coding-rules-loop-result\s*(.*?)\s*-->", re.DOTALL)
MAX_CHECK_RESULT_CHARS = 16_384


class AdapterError(ValueError):
    """Codex Hook 输入不符合当前官方事件结构。"""


def extract_check_result(message: Any) -> Optional[Dict[str, Any]]:
    """从候选回复的隐藏标记中提取当前 diff 的结构化检查结果。"""

    if not isinstance(message, str):
        return None
    matches = CHECK_RESULT_PATTERN.findall(message)
    if not matches:
        return None
    payload = matches[-1].strip()
    if len(payload) > MAX_CHECK_RESULT_CHARS:
        raise AdapterError("Coding Rules Loop 检查结果超过大小限制")
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as error:
        raise AdapterError(f"Coding Rules Loop 检查结果不是有效 JSON：{error.msg}") from error
    if not isinstance(result, dict):
        raise AdapterError("Coding Rules Loop 检查结果必须是 JSON 对象")
    return result


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _repository_root(cwd: Path) -> Optional[Path]:
    try:
        return find_repository_root(str(cwd))
    except (WorkspaceError, OSError):
        return None


def extract_files(hook_input: Dict[str, Any]) -> List[str]:
    cwd = Path(hook_input.get("cwd") or ".").expanduser().resolve()
    repository_root = _repository_root(cwd)
    tool_input = hook_input.get("tool_input") if isinstance(hook_input.get("tool_input"), dict) else {}
    command = tool_input.get("command") if isinstance(tool_input.get("command"), str) else ""
    candidates: Set[str] = set(path_values(tool_input))

    if hook_input.get("tool_name") == "apply_patch":
        candidates.update(PATCH_FILE_PATTERN.findall(command))
        candidates.update(PATCH_MOVE_PATTERN.findall(command))
    elif hook_input.get("tool_name") == "Bash":
        candidates.update(command_file_values(command))

    files = {
        normalized
        for candidate in candidates
        if (normalized := normalize_path(candidate.strip(), cwd, repository_root)) is not None
    }
    return sorted(files)


def is_write_event(hook_input: Dict[str, Any], files: List[str]) -> bool:
    tool_name = str(hook_input.get("tool_name") or "")
    tool_input = hook_input.get("tool_input") if isinstance(hook_input.get("tool_input"), dict) else {}
    command = tool_input.get("command") if isinstance(tool_input.get("command"), str) else ""
    if tool_name == "apply_patch":
        return bool(files)
    if tool_name == "Bash":
        return is_write_command(command, bool(files))
    return bool(files and WRITE_TOOL_PATTERN.search(tool_name))


def collect_change_hints(hook_input: Dict[str, Any], files: Iterable[str]) -> List[str]:
    text_parts = []
    for field in ("prompt",):
        if isinstance(hook_input.get(field), str):
            text_parts.append(hook_input[field])
    tool_input = hook_input.get("tool_input")
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        text_parts.append(tool_input["command"])
    return classify_change_hints(text_parts, files)


class CodexHookAdapter:
    """保持 Hook 协议适配轻量，状态、指纹和修复判断全部交给公共 Core。"""

    def __init__(
        self,
        core: Optional[RulesLoopCore] = None,
        data_home: Optional[Path] = None,
        mode: Optional[str] = None,
        legacy_hook_active: Optional[bool] = None,
    ):
        configured_home = os.environ.get("CODING_RULES_LOOP_HOME")
        self.data_home = Path(data_home or configured_home or (Path.home() / ".coding-rules-loop")).expanduser()
        rules_root = self.data_home / "rules"
        skills_root = Path(os.environ.get("CODING_RULES_LOOP_SKILLS_ROOT") or (Path.home() / ".agents" / "skills"))
        self.core = core or RulesLoopCore(rules_root=rules_root, skills_root=skills_root, data_home=self.data_home)
        self.mode = mode or os.environ.get("CODING_RULES_LOOP_MODE", "observe")
        self.legacy_hook_active = (
            legacy_hook_active
            if legacy_hook_active is not None
            else os.environ.get("CODING_RULES_LOOP_LEGACY_HOOK_ACTIVE") == "1"
        )

    def normalize(self, hook_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(hook_input, dict):
            raise AdapterError("Hook 输入必须是 JSON 对象")
        event_name = hook_input.get("hook_event_name")
        if event_name not in {"SessionStart", *EVENT_MAP}:
            return None
        self._validate_input(hook_input, event_name)
        if event_name == "SessionStart":
            source = hook_input.get("source", "startup")
            internal_event = "context_restored" if source in {"resume", "compact"} else "session_initialized"
        else:
            internal_event = EVENT_MAP.get(event_name)
        if internal_event is None:
            return None

        files = extract_files(hook_input)
        if event_name in {"PreToolUse", "PostToolUse"} and not is_write_event(hook_input, files):
            return None

        prompt = hook_input.get("prompt") if isinstance(hook_input.get("prompt"), str) else ""
        normalized = {
            "event": internal_event,
            "repository_root": hook_input.get("cwd"),
            "session_id": hook_input.get("session_id"),
            "turn_id": hook_input.get("turn_id"),
            "agent_id": hook_input.get("agent_id") or "main",
            "tool": hook_input.get("tool_name") or "Codex",
            "files": files,
            "change_hints": collect_change_hints(hook_input, files),
            "mode": self.mode,
        }
        if event_name == "SessionStart":
            normalized["reason"] = hook_input.get("source", "startup")
        elif event_name == "SessionEnd":
            normalized["reason"] = hook_input.get("reason", "other")
        elif event_name == "UserPromptSubmit":
            normalized["coding_intent"] = classify_coding_intent(prompt)
            normalized["user_decision_received"] = classify_user_decision(prompt)
        elif event_name == "SubagentStart":
            agent_type = str(hook_input.get("agent_type") or "").lower()
            normalized["coding_agent"] = not any(token in agent_type for token in ("explorer", "research", "read-only"))
        elif event_name == "Stop":
            normalized["stop_hook_active"] = hook_input.get("stop_hook_active", False)
            try:
                check_result = extract_check_result(hook_input.get("last_assistant_message"))
            except AdapterError as error:
                normalized["check_result_error"] = str(error)
                check_result = None
            if check_result is not None:
                normalized["check_result"] = check_result

        if event_name in {"SessionStart", "UserPromptSubmit", "SubagentStart", "PreToolUse", "PostToolUse"}:
            normalized["rules_context_delivered"] = True
        return normalized

    def _validate_input(self, hook_input: Dict[str, Any], event_name: str) -> None:
        for field in ("session_id", "cwd"):
            if not isinstance(hook_input.get(field), str) or not hook_input[field]:
                raise AdapterError(f"{event_name} 缺少字符串字段 {field}")

        if event_name not in {"SessionStart", "SessionEnd"}:
            if not isinstance(hook_input.get("turn_id"), str) or not hook_input["turn_id"]:
                raise AdapterError(f"{event_name} 缺少字符串字段 turn_id")
        if event_name == "SessionStart" and hook_input.get("source") not in {"startup", "resume", "clear", "compact"}:
            raise AdapterError("SessionStart.source 非法")
        if event_name == "SessionEnd" and hook_input.get("reason", "other") != "other":
            raise AdapterError("SessionEnd.reason 非法")
        if event_name in {"PreToolUse", "PostToolUse"}:
            if not isinstance(hook_input.get("tool_name"), str) or not hook_input["tool_name"]:
                raise AdapterError(f"{event_name} 缺少字符串字段 tool_name")
            if not isinstance(hook_input.get("tool_input"), dict):
                raise AdapterError(f"{event_name}.tool_input 必须是 JSON 对象")
        if event_name == "UserPromptSubmit" and not isinstance(hook_input.get("prompt"), str):
            raise AdapterError("UserPromptSubmit.prompt 必须是字符串")
        if event_name == "SubagentStart":
            for field in ("agent_id", "agent_type"):
                if not isinstance(hook_input.get(field), str) or not hook_input[field]:
                    raise AdapterError(f"SubagentStart 缺少字符串字段 {field}")
        if event_name == "Stop" and not isinstance(hook_input.get("stop_hook_active"), bool):
            raise AdapterError("Stop.stop_hook_active 必须是布尔值")
        if event_name == "Interrupt" and not isinstance(hook_input.get("permission_mode"), str):
            raise AdapterError("Interrupt.permission_mode 必须是字符串")

    def handle(self, hook_input: Dict[str, Any]) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            normalized = self.normalize(hook_input)
        except (AdapterError, OSError, TypeError) as error:
            return {"systemMessage": f"Coding Rules Loop {self.mode} 输入无效，已放行：{error}"}
        if normalized is None:
            return {}

        result = self.core.process(normalized)
        observation_error = self._record_observation(hook_input, result, started)
        if result.get("status") == "check_failed":
            message = f"Coding Rules Loop {self.mode} 执行失败，已放行：" + "; ".join(result.get("warnings", []))
            return {"systemMessage": message}
        if observation_error:
            return {"systemMessage": f"Coding Rules Loop {self.mode} 记录失败，已放行：{observation_error}"}

        event_name = hook_input.get("hook_event_name")
        if event_name == "Stop":
            return self._stop_output(result)
        if result.get("load_required") and event_name in {
            "SessionStart",
            "UserPromptSubmit",
            "SubagentStart",
            "PreToolUse",
            "PostToolUse",
        }:
            return self._additional_context(event_name, result)

        return {}

    def _stop_output(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """把 Core 的收尾状态转换为 Codex Stop 续接或正常结束。"""

        if result.get("status") == "check_required":
            return {"decision": "block", "reason": self._check_prompt(result)}
        if result.get("result") == "repairable" and result.get("repair_allowed"):
            return {"decision": "block", "reason": self._repair_prompt(result)}
        return {}

    def _check_prompt(self, result: Dict[str, Any]) -> str:
        diff_hash = str(result.get("current_diff_hash") or "")
        changes = [str(path) for path in result.get("changes", [])]
        checklist = [
            f"[{rule.get('level', 'SHOULD')}] {rule.get('id')}"
            for rule in result.get("semantic_checklist", [])
        ]
        displayed_changes = changes[:40]
        if len(changes) > len(displayed_changes):
            displayed_changes.append(f"... 另有 {len(changes) - len(displayed_changes)} 个相关文件")
        files_text = "\n".join(f"- {path}" for path in displayed_changes) or "- 当前整批相关 diff"
        rules_text = "\n".join(f"- {rule}" for rule in checklist) or "- 当前已加载规则"
        passed_result = json.dumps(
            {
                "diff_hash": diff_hash,
                "tool_status": "passed",
                "semantic_status": "passed",
                "findings": [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        finding_result = json.dumps(
            {
                "diff_hash": diff_hash,
                "tool_status": "passed",
                "semantic_status": "failed",
                "findings": [
                    {
                        "result": "repairable",
                        "rule_id": "实际规则 ID",
                        "issue_type": "实际问题类型",
                        "file": "实际相对路径",
                        "suggestion": "最小必要修复",
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        mode_instruction = (
            "发现明确且安全的问题时先用 repairable findings 回写，不在本检查回合直接修改；"
            "Core 通过循环保护后会继续要求修复。"
            if result.get("mode") == "repair"
            else "observe 不启动额外自动修复轮次；若仍有明确问题，回写 repairable 并在正常总结中说明。"
        )
        notices = {
            "stale_check_result": "\n上一次检查结果对应旧 diff，本次必须按新指纹重新检查。",
            "invalid_check_result": "\n上一次结构化检查结果无效，请按本提示中的 JSON 格式重新回写。",
        }
        check_notice = notices.get(result.get("reason"), "")
        return (
            "Coding Rules Loop 检测到当前 diff 尚未完成收尾检查，请继续执行，不要直接结束。\n"
            f"当前模式：{result.get('mode', self.mode)}\n"
            f"当前 diff 指纹：{diff_hash}{check_notice}\n\n"
            "相关文件：\n"
            f"{files_text}\n\n"
            "语义检查清单：\n"
            f"{rules_text}\n\n"
            "请只检查当前任务相关 diff，核对规则、执行最小必要验证，并按以下口径处理：\n"
            f"1. {mode_instruction}\n"
            "2. 业务、兼容、数据影响或规则冲突存在歧义时，向用户提出具体问题。\n"
            "3. 验证或检查失败时不得伪装成通过。\n"
            "4. 在正常用户回复末尾追加一个 Markdown 不展示的检查结果注释；只写 JSON，不使用代码围栏。\n\n"
            "检查通过时追加：\n"
            f"<!-- coding-rules-loop-result\n{passed_result}\n-->\n\n"
            "发现明确问题时追加：\n"
            f"<!-- coding-rules-loop-result\n{finding_result}\n-->\n\n"
            "存在歧义时将 finding.result 改为 needs_user_decision 并增加 question；检查器或验证环境失败时使用 "
            "check_failed。每项都要填写真实 rule_id、issue_type 和 file，不保留示例占位值。"
        )

    def _repair_prompt(self, result: Dict[str, Any]) -> str:
        items = []
        for finding in result.get("findings", []):
            items.append(
                "- {rule}；文件：{file}；建议：{suggestion}".format(
                    rule=finding.get("rule_id") or "未标注规则",
                    file=finding.get("file") or "当前相关 diff",
                    suggestion=finding.get("suggestion") or "按已加载规则完成最小必要修复",
                )
            )
        findings_text = "\n".join(items) or "- 按已加载规则完成最小必要修复"
        return (
            "Coding Rules Loop 已确认以下问题可以修复，请继续处理，不要直接结束：\n"
            f"{findings_text}\n"
            "修复后执行最小必要验证。代码 diff 变化后不要复用旧检查结果，下一次 Stop 会按新指纹重新检查。"
        )

    def _additional_context(self, event_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
        assets = "\n".join(f"- {path}" for path in result.get("assets_to_load", []))
        context = (
            f"Coding Rules Loop 当前为 {result.get('mode', self.mode)}。写入前请完整读取以下尚未加载的规则资产；"
            "同一 loaded_rules_hash 在连续上下文中不重复读取：\n"
            f"{assets}\n"
            "只处理当前任务相关代码；结束前按整批相关 diff 检查、修复明确问题并执行最小必要验证。"
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": context,
            }
        }

    def _record_observation(
        self,
        hook_input: Dict[str, Any],
        result: Dict[str, Any],
        started: float,
    ) -> Optional[str]:
        try:
            observation_root = self.data_home / "observations"
            observation_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = observation_root / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl")
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tool": "codex",
                "repository_hash": _sha256(str(hook_input.get("cwd") or ""))[:20],
                "session_hash": _sha256(str(hook_input.get("session_id") or ""))[:20],
                "event": hook_input.get("hook_event_name"),
                "mode": result.get("mode", self.mode),
                "status": result.get("status"),
                "result": result.get("result"),
                "active_skills": result.get("required_skills", []),
                "active_references": result.get("required_references", []),
                "api_boundary": result.get("api_boundary"),
                "load_required": result.get("load_required", False),
                "would_repair": result.get("would_repair", False),
                "reason": result.get("reason"),
                "tool_status": result.get("check", {}).get("tool_status"),
                "semantic_status": result.get("check", {}).get("semantic_status"),
                "continuation_count": result.get("check", {}).get("continuation_count", 0),
                "changed_file_count": len(result.get("changes", [])),
                "semantic_rule_count": len(result.get("semantic_checklist", [])),
                "legacy_hook_active": self.legacy_hook_active,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            descriptor = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, encoded)
            finally:
                os.close(descriptor)
            return None
        except OSError as error:
            return str(error)


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
        if not isinstance(hook_input, dict):
            raise ValueError("Hook 输入必须是 JSON 对象")
        output = CodexHookAdapter().handle(hook_input)
    except (json.JSONDecodeError, OSError, ValueError, TypeError) as error:
        output = {"systemMessage": f"Coding Rules Loop observe 输入无效，已放行：{error}"}
    json.dump(output, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
