#!/usr/bin/env python3
"""将 Qoder IDE Hook 输入转换为 Coding Rules Loop 统一事件。"""

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
    "PostToolUseFailure": "after_code_write",
    "Stop": "before_task_finish",
    "PreCompact": "context_restored",
}
SHELL_TOOLS = {"Bash", "run_in_terminal"}
WRITE_TOOL_PATTERN = re.compile(r"(?:write|edit|search_replace|create_file|delete|move|rename|format)", re.IGNORECASE)


class AdapterError(ValueError):
    """Qoder IDE Hook 输入不符合当前官方事件结构。"""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _repository_root(cwd: Path) -> Optional[Path]:
    try:
        return find_repository_root(str(cwd))
    except (WorkspaceError, OSError):
        return None


def discover_qoder_rules(repository_root: Optional[Path]) -> List[str]:
    """Qoder 项目 Rules 高于 AGENTS.md，必须参与当前规则组合与指纹。"""

    if repository_root is None:
        return []
    rules_root = repository_root / ".qoder" / "rules"
    if not rules_root.is_dir():
        return []
    return sorted(
        str(path.relative_to(repository_root))
        for path in rules_root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".mdc"}
    )


def extract_files(hook_input: Dict[str, Any]) -> List[str]:
    cwd = Path(hook_input.get("cwd") or ".").expanduser().resolve()
    repository_root = _repository_root(cwd)
    tool_input = hook_input.get("tool_input") if isinstance(hook_input.get("tool_input"), dict) else {}
    command = tool_input.get("command") if isinstance(tool_input.get("command"), str) else ""
    candidates: Set[str] = set(path_values(tool_input))
    if hook_input.get("tool_name") in SHELL_TOOLS:
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
    if tool_name in SHELL_TOOLS:
        return is_write_command(command, bool(files))
    return bool(files and WRITE_TOOL_PATTERN.search(tool_name))


def collect_change_hints(hook_input: Dict[str, Any], files: Iterable[str]) -> List[str]:
    text_parts = []
    if isinstance(hook_input.get("prompt"), str):
        text_parts.append(hook_input["prompt"])
    tool_input = hook_input.get("tool_input")
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        text_parts.append(tool_input["command"])

    tool_name = str(hook_input.get("tool_name") or "").lower()
    operation = "add" if "create" in tool_name else "update"
    text_parts.extend(f"*** {operation} file: {path}" for path in files)
    return classify_change_hints(text_parts, files)


class QoderHookAdapter:
    """只处理 Qoder IDE 事件差异，公共行为继续委托给 RulesLoopCore。"""

    def __init__(
        self,
        core: Optional[RulesLoopCore] = None,
        data_home: Optional[Path] = None,
        mode: Optional[str] = None,
    ):
        configured_home = os.environ.get("CODING_RULES_LOOP_HOME")
        self.data_home = Path(data_home or configured_home or (Path.home() / ".coding-rules-loop")).expanduser()
        rules_root = self.data_home / "rules"
        skills_root = Path(os.environ.get("CODING_RULES_LOOP_SKILLS_ROOT") or (Path.home() / ".qoder" / "skills"))
        self.core = core or RulesLoopCore(rules_root=rules_root, skills_root=skills_root, data_home=self.data_home)
        self.mode = mode or os.environ.get("CODING_RULES_LOOP_MODE", "observe")

    def normalize(self, hook_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(hook_input, dict):
            raise AdapterError("Hook 输入必须是 JSON 对象")
        event_name = hook_input.get("hook_event_name")
        if event_name not in {"SessionStart", *EVENT_MAP}:
            return None
        self._validate_input(hook_input, event_name)
        internal_event = "session_initialized" if event_name == "SessionStart" else EVENT_MAP[event_name]

        files = extract_files(hook_input)
        if event_name in {"PreToolUse", "PostToolUse", "PostToolUseFailure"} and not is_write_event(hook_input, files):
            return None

        prompt = hook_input.get("prompt") if isinstance(hook_input.get("prompt"), str) else ""
        repository_root = _repository_root(Path(hook_input["cwd"]).expanduser().resolve())
        normalized = {
            "event": internal_event,
            "repository_root": hook_input.get("cwd"),
            "session_id": hook_input.get("session_id"),
            "turn_id": None,
            "agent_id": hook_input.get("agent_id") or "main",
            "tool": hook_input.get("tool_name") or "Qoder IDE",
            "files": files,
            "change_hints": collect_change_hints(hook_input, files),
            "project_rule_files": discover_qoder_rules(repository_root),
            "mode": self.mode,
        }
        if event_name == "SessionStart":
            normalized["reason"] = hook_input.get("type", "startup")
        elif event_name == "SessionEnd":
            normalized["reason"] = hook_input.get("reason", "exit")
        elif event_name == "UserPromptSubmit":
            normalized["coding_intent"] = classify_coding_intent(prompt)
            normalized["user_decision_received"] = classify_user_decision(prompt)
        elif event_name == "SubagentStart":
            agent_type = str(hook_input.get("agent_type") or "").lower()
            normalized["coding_agent"] = not any(token in agent_type for token in ("explorer", "research", "read-only"))
        elif event_name == "PreCompact":
            normalized["reason"] = "compact"
            normalized["invalidate_rules_context"] = True

        if event_name in {"SessionStart", "UserPromptSubmit", "SubagentStart", "PreToolUse"}:
            normalized["rules_context_delivered"] = True
        return normalized

    def _validate_input(self, hook_input: Dict[str, Any], event_name: str) -> None:
        for field in ("session_id", "cwd"):
            if not isinstance(hook_input.get(field), str) or not hook_input[field]:
                raise AdapterError(f"{event_name} 缺少字符串字段 {field}")
        if event_name == "SessionStart" and hook_input.get("type") != "startup":
            raise AdapterError("Qoder IDE 当前只确认 SessionStart.type=startup")
        if event_name == "SessionEnd" and not isinstance(hook_input.get("reason"), str):
            raise AdapterError("SessionEnd.reason 必须是字符串")
        if event_name in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}:
            if not isinstance(hook_input.get("tool_name"), str) or not hook_input["tool_name"]:
                raise AdapterError(f"{event_name} 缺少字符串字段 tool_name")
            if not isinstance(hook_input.get("tool_input"), dict):
                raise AdapterError(f"{event_name}.tool_input 必须是 JSON 对象")
        if event_name == "PostToolUseFailure" and not isinstance(hook_input.get("error"), str):
            raise AdapterError("PostToolUseFailure.error 必须是字符串")
        if event_name == "UserPromptSubmit" and not isinstance(hook_input.get("prompt"), str):
            raise AdapterError("UserPromptSubmit.prompt 必须是字符串")
        if event_name == "SubagentStart":
            for field in ("agent_id", "agent_type"):
                if not isinstance(hook_input.get(field), str) or not hook_input[field]:
                    raise AdapterError(f"SubagentStart 缺少字符串字段 {field}")
        if event_name == "Stop" and not isinstance(hook_input.get("stop_hook_active"), bool):
            raise AdapterError("Stop.stop_hook_active 必须是布尔值")
        if event_name == "PreCompact" and hook_input.get("trigger") not in {"manual", "auto"}:
            raise AdapterError("PreCompact.trigger 必须是 manual 或 auto")

    def handle(self, hook_input: Dict[str, Any]) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            normalized = self.normalize(hook_input)
        except (AdapterError, OSError, TypeError) as error:
            return self._failure_output(str(hook_input.get("hook_event_name") or ""), str(error))
        if normalized is None:
            return {}

        result = self.core.process(normalized)
        observation_error = self._record_observation(hook_input, result, started)
        if result.get("status") == "check_failed":
            return self._failure_output(
                str(hook_input.get("hook_event_name") or ""),
                "; ".join(result.get("warnings", [])),
            )
        if observation_error:
            return self._failure_output(str(hook_input.get("hook_event_name") or ""), observation_error)

        event_name = str(hook_input.get("hook_event_name") or "")
        if result.get("load_required") and event_name in {"SessionStart", "UserPromptSubmit", "SubagentStart", "PreToolUse"}:
            return self._additional_context(event_name, result)

        # Qoder observe 的 Stop 不阻断；PostToolUse 也不输出伪造的修复反馈。
        return {}

    def _additional_context(self, event_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
        assets = "\n".join(f"- {path}" for path in result.get("assets_to_load", []))
        context = (
            "Coding Rules Loop 当前为 observe。写入前请完整读取以下尚未加载的规则资产；"
            "同一 loaded_rules_hash 在连续上下文中不重复读取：\n"
            f"{assets}\n"
            "只处理当前任务相关代码；结束前按整批相关 diff 检查、修复明确问题并执行最小必要验证。"
        )
        return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}}

    def _failure_output(self, event_name: str, message: str) -> Dict[str, Any]:
        warning = f"Coding Rules Loop Qoder observe 执行失败，已放行：{message}"
        if event_name in {"SessionStart", "UserPromptSubmit", "SubagentStart", "PreToolUse"}:
            return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": warning}}
        if event_name == "PostToolUse":
            return {"hookSpecificOutput": {"hookEventName": event_name, "feedback": warning}}
        return {}

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
                "tool": "qoder-ide",
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
                "changed_file_count": len(result.get("changes", [])),
                "semantic_rule_count": len(result.get("semantic_checklist", [])),
                "legacy_hook_active": False,
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
        output = QoderHookAdapter().handle(hook_input)
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        output = {}
    json.dump(output, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
