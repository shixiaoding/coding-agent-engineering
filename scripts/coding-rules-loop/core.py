#!/usr/bin/env python3
"""Coding Rules Loop 的工具无关编排核心。

本模块只处理统一事件、规则选择和检查结果编排，不解析 Codex、Qoder、Claude
各自的 Hook 协议。工具差异由后续 Adapter 转换成这里约定的 JSON 输入。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from state import RESULTS, StateError, StateStore, evaluate_findings, invalidate_check, reset_repair, transition_state
from workspace import (
    WorkspaceError,
    NotGitRepository,
    is_implementation_path,
    calculate_task_diff_hash,
    collect_changes,
    file_hash_map,
    find_repository_root,
    snapshot_workspace,
)


SUPPORTED_EVENTS = {
    "session_initialized",
    "context_restored",
    "session_ended",
    "task_intent_received",
    "agent_started",
    "before_code_write",
    "after_code_write",
    "before_task_finish",
    "turn_interrupted",
}
MODES = {"observe", "repair"}
MAX_CHECK_CONTINUATIONS = 3
RULE_HEADING = re.compile(r"^## \[(MUST|SHOULD)\] (\S+)\s*$", re.MULTILINE)
ENABLED_SKILL = re.compile(r"^- `([^`]+)`(?:：|:)", re.MULTILINE)

JAVA_REFERENCE_HINTS = {
    "new_module",
    "version_change",
    "architecture_change",
    "layering_change",
    "transaction_change",
    "concurrency_change",
    "performance_change",
    "test_strategy_change",
}
API_REFERENCE_HINTS = {
    "new_endpoint",
    "contract_change",
    "request_change",
    "response_change",
    "openapi_change",
    "authentication_change",
    "authorization_change",
    "pagination_change",
    "error_contract_change",
    "api_policy_unclear",
}


class CoreError(RuntimeError):
    """统一输入或规则资产无效时抛出。"""


def _hash_files(files: Sequence[Path]) -> str:
    """计算规则资产组合的内容指纹，使规则变化能够让旧检查结果失效。"""

    digest = hashlib.sha256()
    for path in sorted({file.resolve() for file in files}, key=lambda item: str(item)):
        if not path.is_file():
            raise CoreError(f"规则资产不存在：{path}")
        encoded_path = str(path).encode("utf-8", "surrogateescape")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _relative_asset(path: Path, roots: Sequence[Path]) -> str:
    resolved = path.resolve()
    for root in roots:
        try:
            return str(resolved.relative_to(root.resolve()))
        except ValueError:
            continue
    return str(resolved)


def _is_java(path: str) -> bool:
    return path.lower().endswith(".java")


def _is_openapi(path: str) -> bool:
    name = Path(path).name.lower()
    return ("openapi" in name or "swagger" in name) and name.endswith((".yml", ".yaml", ".json"))


def _is_feign(path: str) -> bool:
    name = Path(path).stem.lower()
    return _is_java(path) and "feign" in name


def _is_api_java(path: str) -> bool:
    if not _is_java(path):
        return False
    name = Path(path).stem.lower()
    return any(token in name for token in ("controller", "request", "response")) or name.endswith(("req", "rsp"))


def _active_languages(files: Iterable[str], skills: Iterable[str]) -> List[str]:
    languages = set()
    if "coding-standards-java" in skills:
        languages.add("java")
    suffix_languages = {
        ".go": "go",
        ".js": "javascript",
        ".kt": "kotlin",
        ".php": "php",
        ".py": "python",
        ".ts": "typescript",
    }
    for value in files:
        language = suffix_languages.get(Path(value).suffix.lower())
        if language:
            languages.add(language)
    return sorted(languages)


def _discover_project_rules(repository_root: Path, files: Iterable[str], explicit: Iterable[str]) -> List[Path]:
    """收集根目录和触达文件沿途生效的项目级 AGENTS.md。"""

    candidates: Set[Path] = set()
    root_rule = repository_root / "AGENTS.md"
    if root_rule.is_file():
        candidates.add(root_rule.resolve())

    for relative_path in files:
        current = (repository_root / relative_path).resolve().parent
        while True:
            try:
                current.relative_to(repository_root)
            except ValueError:
                break
            local_rule = current / "AGENTS.md"
            if local_rule.is_file():
                candidates.add(local_rule.resolve())
            if current == repository_root:
                break
            current = current.parent

    for value in explicit:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = repository_root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(repository_root)
        except ValueError as error:
            raise CoreError(f"项目规则必须位于当前仓库内：{candidate}") from error
        if not candidate.is_file():
            raise CoreError(f"项目规则不存在：{candidate}")
        candidates.add(candidate)
    return sorted(candidates, key=lambda item: str(item))


class RuleSelector:
    """根据整批相关文件选择 Common、专业 Skill 和按需 reference。"""

    def __init__(self, rules_root: Path, skills_root: Path):
        self.rules_root = rules_root.expanduser().resolve()
        self.skills_root = skills_root.expanduser().resolve()

    def _common_file(self) -> Path:
        common = self.rules_root / "coding-rules.md"
        if not common.is_file():
            raise CoreError(f"缺少全局 Common 规则：{common}")
        return common

    def _enabled_skills(self, common: Path) -> Set[str]:
        return set(ENABLED_SKILL.findall(common.read_text(encoding="utf-8")))

    def select(
        self,
        repository_root: Path,
        files: Iterable[str],
        change_hints: Iterable[str] = (),
        requested_skills: Iterable[str] = (),
        requested_references: Iterable[str] = (),
        prior_skills: Iterable[str] = (),
        prior_references: Iterable[str] = (),
        project_rule_files: Iterable[str] = (),
    ) -> Dict[str, Any]:
        """返回当前任务的有效规则组合；同批文件取并集但保留逐文件 API 边界。"""

        normalized_files = []
        for value in files:
            if not value:
                continue
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise CoreError(f"文件路径必须是仓库内相对路径：{value}")
            normalized_files.append(str(path))
        normalized_files = sorted(set(normalized_files))
        hints = set(change_hints)
        common = self._common_file()
        enabled = self._enabled_skills(common)
        skills = set(prior_skills) | set(requested_skills)

        if any(_is_java(path) for path in normalized_files):
            skills.add("coding-standards-java")
        api_paths = [path for path in normalized_files if _is_api_java(path) or _is_openapi(path) or _is_feign(path)]
        if api_paths:
            skills.add("api-design")
        unknown = skills - enabled
        if unknown:
            raise CoreError(f"请求了未启用的专业 Skill：{', '.join(sorted(unknown))}")

        references = set(prior_references) | set(requested_references)
        if "coding-standards-java" in skills and hints & JAVA_REFERENCE_HINTS:
            references.add("coding-standards-java/references/java-spec.md")
        if "api-design" in skills and (hints & API_REFERENCE_HINTS or any(_is_openapi(path) for path in normalized_files)):
            references.add("api-design/references/api-spec.md")

        # 总边界用于快速判断，逐文件边界用于防止同批 Controller 让 Feign 套用新接口规范。
        non_feign_api = any((_is_api_java(path) and not _is_feign(path)) or _is_openapi(path) for path in api_paths)
        if not api_paths:
            api_boundary: Optional[str] = None
        elif non_feign_api:
            api_boundary = "full"
        else:
            api_boundary = "compatibility-only"
        api_file_boundaries = {
            path: "compatibility-only" if _is_feign(path) else "full"
            for path in sorted(api_paths)
        }

        for reference in references:
            reference_path = Path(reference)
            if reference_path.is_absolute() or ".." in reference_path.parts or not reference_path.parts:
                raise CoreError(f"非法专业规范路径：{reference}")
            if reference_path.parts[0] not in skills:
                raise CoreError(f"专业规范不属于当前激活 Skill：{reference}")

        project_rules = _discover_project_rules(repository_root, normalized_files, project_rule_files)
        skill_files = [self.skills_root / skill / "SKILL.md" for skill in sorted(skills)]
        loaded_reference_files = [self.skills_root / reference for reference in sorted(references)]
        # reference 即使本轮未加载也参与规则包指纹，规范更新后必须重新验收现有 diff。
        package_files = list(skill_files)
        for skill in sorted(skills):
            package_files.extend(sorted((self.skills_root / skill / "references").glob("**/*.md")))

        loaded_files = [*project_rules, common, *skill_files, *loaded_reference_files]
        rules_hash = _hash_files([*project_rules, common, *package_files])
        loaded_rules_hash = _hash_files(loaded_files)
        roots = [repository_root, self.rules_root.parent, self.skills_root]
        return {
            "files": normalized_files,
            "common": _relative_asset(common, roots),
            "project_rules": [_relative_asset(path, roots) for path in project_rules],
            "skills": sorted(skills),
            "references": sorted(references),
            "api_boundary": api_boundary,
            "api_file_boundaries": api_file_boundaries,
            "rules_hash": rules_hash,
            "loaded_rules_hash": loaded_rules_hash,
            "loaded_files": [str(path) for path in loaded_files],
        }


def build_semantic_checklist(loaded_files: Iterable[str]) -> List[Dict[str, str]]:
    """从本轮实际加载的 Markdown 中提取稳定规则 ID，供 Agent 语义复核。"""

    rules: Dict[str, str] = {}
    for value in loaded_files:
        path = Path(value)
        if not path.is_file():
            continue
        for level, rule_id in RULE_HEADING.findall(path.read_text(encoding="utf-8")):
            rules[rule_id] = level
    return [{"id": rule_id, "level": rules[rule_id]} for rule_id in sorted(rules)]


class RulesLoopCore:
    """协调规则选择、会话状态、工作区指纹和检查—修复状态机。"""

    def __init__(
        self,
        rules_root: Optional[Path] = None,
        skills_root: Optional[Path] = None,
        data_home: Optional[Path] = None,
    ):
        configured_home = os.environ.get("CODING_RULES_LOOP_HOME")
        self.data_home = Path(data_home or configured_home or (Path.home() / ".coding-rules-loop")).expanduser()
        self.rules_root = Path(rules_root or (self.data_home / "rules"))
        self.skills_root = Path(skills_root or (Path.home() / ".agents" / "skills"))
        self.selector = RuleSelector(self.rules_root, self.skills_root)
        self.store = StateStore(self.data_home)

    def _validate_event(self, event: Dict[str, Any]) -> Tuple[str, Path, str, str]:
        if not isinstance(event, dict):
            raise CoreError("统一事件必须是 JSON 对象")
        event_name = event.get("event")
        if event_name not in SUPPORTED_EVENTS:
            raise CoreError(f"未知统一事件：{event_name}")
        if not event.get("repository_root"):
            raise CoreError("缺少 repository_root")
        if not event.get("session_id"):
            raise CoreError("缺少 session_id")
        mode = event.get("mode", "observe")
        if mode not in MODES:
            raise CoreError(f"未知运行模式：{mode}")
        for field in (
            "files",
            "change_hints",
            "requested_skills",
            "requested_references",
            "project_rule_files",
        ):
            value = event.get(field, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise CoreError(f"{field} 必须是字符串数组")
        if event.get("check_result") is not None and not isinstance(event["check_result"], dict):
            raise CoreError("check_result 必须是 JSON 对象")
        root = find_repository_root(event["repository_root"])
        return event_name, root, str(event["session_id"]), mode

    @staticmethod
    def _skipped(event: Dict[str, Any], reason: str) -> Dict[str, Any]:
        return {
            "event": event.get("event"),
            "mode": event.get("mode", "observe"),
            "status": "skipped",
            "result": "skipped",
            "reason": reason,
            "load_required": False,
            "assets_to_load": [],
            "required_skills": [],
            "required_references": [],
            "changes": [],
            "semantic_checklist": [],
            "would_repair": False,
            "warnings": [],
        }

    def process(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """处理一个统一事件并返回可直接序列化的 JSON 结果。"""

        started = time.perf_counter()
        warnings: List[str] = []
        try:
            # 1、校验输入并处理会话级清理，业务仓库内容只读不写。
            event_name, repository_root, session_id, requested_mode = self._validate_event(event)
            event = dict(event)
            supplied_files = event.get("files", [])
            event["files"] = [path for path in supplied_files if is_implementation_path(path)]
            if event_name in {"before_code_write", "after_code_write"} and supplied_files and not event["files"]:
                return self._skipped(event, "non_code_change")
            if event_name == "session_initialized" and event.get("reason") == "clear":
                self.store.clear(str(repository_root), session_id)
            if event_name == "session_initialized" and event.get("reason", "startup") == "startup":
                self.store.cleanup()

            holder: Dict[str, Any] = {}

            def update(state: Dict[str, Any]) -> Dict[str, Any]:
                # 2、在同一状态锁内合并文件、规则和 Agent 状态，避免并发覆盖。
                if "mode_override" in event:
                    override = event.get("mode_override")
                    if override is not None and override not in MODES:
                        raise CoreError(f"未知会话模式覆盖：{override}")
                    state["mode_override"] = override
                mode = state.get("mode_override") or requested_mode
                payload = dict(event)
                if state.get("baseline") is None and (
                    event_name == "before_code_write"
                    or (event_name == "task_intent_received" and event.get("coding_intent"))
                ):
                    payload["baseline"] = snapshot_workspace(str(repository_root))
                updated = transition_state(state, event_name, payload)
                turn_coding = updated["current_turn"].get("coding_active", updated["coding_active"])
                coding_agent = event_name == "agent_started" and bool(event.get("coding_agent"))
                if not turn_coding and not coding_agent:
                    holder["skip_reason"] = "non_code_task"
                    return updated
                if event_name == "agent_started" and not event.get("coding_agent"):
                    holder["skip_reason"] = "read_only_agent"
                    return updated
                files = sorted(set(updated.get("touched_files", [])) | set(event.get("files", [])))
                hints = sorted(set(updated.get("change_hints", [])) | set(event.get("change_hints", [])))
                selection = self.selector.select(
                    repository_root=repository_root,
                    files=files,
                    change_hints=hints,
                    requested_skills=event.get("requested_skills", []),
                    requested_references=event.get("requested_references", []),
                    prior_skills=state.get("active_skills", []),
                    prior_references=state.get("active_references", []),
                    project_rule_files=event.get("project_rule_files", []),
                )
                updated["active_skills"] = selection["skills"]
                updated["active_references"] = selection["references"]
                updated["change_hints"] = hints
                if event_name == "after_code_write" and event.get("agent_id"):
                    agent = updated["agents"][event["agent_id"]]
                    agent["active_skills"] = sorted(set(agent["active_skills"]) | set(selection["skills"]))
                updated["rules_hash"] = selection["rules_hash"]
                if event.get("invalidate_rules_context"):
                    # Qoder IDE 只有 PreCompact，没有可注入上下文的 PostCompact；先失效，后续事件再加载。
                    updated["loaded_rules_hash"] = None
                    invalidated_agent_id = event.get("agent_id")
                    if invalidated_agent_id in updated.get("agents", {}):
                        updated["agents"][invalidated_agent_id]["loaded_rules_hash"] = None
                should_load = (
                    event_name in {"before_code_write", "after_code_write", "before_task_finish"}
                    or (event_name == "task_intent_received" and bool(event.get("coding_intent")))
                    or (event_name == "agent_started" and bool(event.get("coding_agent")))
                    or (event_name == "context_restored" and state.get("coding_active"))
                )
                force_reload = (
                    should_load
                    and event_name == "context_restored"
                    and event.get("reason") in {"compact", "resume"}
                )
                agent_id = event.get("agent_id")
                agent_loaded_hash = (
                    updated.get("agents", {}).get(agent_id, {}).get("loaded_rules_hash")
                    if agent_id
                    else updated.get("loaded_rules_hash")
                )
                explicit_loaded_hash = event.get("loaded_rules_hash")
                if explicit_loaded_hash == selection["loaded_rules_hash"]:
                    agent_loaded_hash = explicit_loaded_hash
                load_required = should_load and (
                    force_reload or agent_loaded_hash != selection["loaded_rules_hash"]
                )
                acknowledged_hash = explicit_loaded_hash
                if event.get("rules_context_delivered") and load_required:
                    acknowledged_hash = selection["loaded_rules_hash"]
                if acknowledged_hash == selection["loaded_rules_hash"]:
                    # Adapter 已发送加载上下文或明确回传同一指纹后，当前 Agent 才记为已加载。
                    updated["loaded_rules_hash"] = acknowledged_hash
                    if agent_id:
                        agent = updated["agents"].setdefault(
                            agent_id,
                            {
                                "touched_files": [],
                                "active_skills": [],
                                "loaded_rules_hash": None,
                                "last_updated_at": updated["last_updated_at"],
                            },
                        )
                        agent["loaded_rules_hash"] = acknowledged_hash
                holder.update(
                    {
                        "selection": selection,
                        "load_required": load_required,
                        "force_reload": force_reload,
                        "mode": mode,
                    }
                )

                if event_name == "before_task_finish":
                    self._finish(updated, repository_root, selection, event, mode, holder)
                return updated

            state, state_warnings = self.store.update(str(repository_root), session_id, update)
            warnings.extend(state_warnings)
            if holder.get("skip_reason"):
                response = self._skipped(event, holder["skip_reason"])
                response["warnings"] = warnings
                return response
            selection = holder["selection"]

            # 3、输出只包含路径、指纹、状态和脱敏指标，不回传源码或完整提示。
            response = {
                "event": event_name,
                "status": holder.get("status", "ready"),
                "mode": holder["mode"],
                "active_languages": _active_languages(selection["files"], selection["skills"]),
                "required_skills": selection["skills"],
                "required_references": selection["references"],
                "api_boundary": selection["api_boundary"],
                "api_file_boundaries": selection["api_file_boundaries"],
                "rules_hash": selection["rules_hash"],
                "loaded_rules_hash": selection["loaded_rules_hash"],
                "load_required": holder["load_required"],
                "assets_to_load": selection["loaded_files"] if holder["load_required"] else [],
                "check": state["check"],
                "repair": state["repair"],
                "warnings": warnings,
            }
            response.update(holder.get("finish_response", {}))
            response["metrics"] = {
                "event": event_name,
                "mode": holder["mode"],
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "active_skills": selection["skills"],
                "load_required": holder["load_required"],
            }
            return response
        except NotGitRepository:
            return self._skipped(event, "not_git_repository")
        except (CoreError, StateError, WorkspaceError, OSError, ValueError, TypeError, KeyError) as error:
            event_name = event.get("event") if isinstance(event, dict) else None
            event_mode = event.get("mode", "observe") if isinstance(event, dict) else "observe"
            return {
                "event": event_name,
                "status": "check_failed",
                "result": "check_failed",
                "reason": "core_failure",
                "warnings": [str(error)],
                "metrics": {
                    "event": event_name,
                    "mode": event_mode,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                },
            }

    def _finish(
        self,
        state: Dict[str, Any],
        repository_root: Path,
        selection: Dict[str, Any],
        event: Dict[str, Any],
        mode: str,
        holder: Dict[str, Any],
    ) -> None:
        """基于整批相关 diff 决定通过、继续检查、建议修复或咨询用户。"""

        if not state.get("coding_active") or state.get("baseline") is None:
            state["check"] = {
                "diff_hash": None,
                "file_hashes": {},
                "tool_status": "passed",
                "semantic_status": "passed",
                "continuation_count": 0,
            }
            holder["status"] = "passed"
            holder["finish_response"] = {
                "result": "passed",
                "changes": [],
                "semantic_checklist": [],
                "task_report": {
                    "changed_file_count": 0,
                    "contains_existing_changes": False,
                    "result": "passed",
                },
            }
            return

        workspace = collect_changes(str(repository_root), state["baseline"], state.get("touched_files", []))
        changes = [
            change for change in workspace["changes"]
            if is_implementation_path(change["path"])
            or (change.get("original_path") and is_implementation_path(change["original_path"]))
        ]
        workspace["contains_existing_changes"] = any(change["existed_before_task"] for change in changes)
        if not changes:
            invalidate_check(state)
            reset_repair(state)
            holder["status"] = "skipped"
            holder["finish_response"] = self._skipped(event, "non_code_change")
            return

        # Shell、格式化器或 IDE 可能没有在事件中报告路径，Stop 以真实变化补齐 Skill 选择。
        changed_paths = sorted({
            path for change in changes
            for path in (change["path"], change.get("original_path"))
            if path
        })
        if not set(changed_paths).issubset(selection["files"]):
            selection = self.selector.select(
                repository_root=repository_root,
                files=[*selection["files"], *changed_paths],
                change_hints=state.get("change_hints", []),
                requested_skills=event.get("requested_skills", []),
                requested_references=event.get("requested_references", []),
                prior_skills=state.get("active_skills", []),
                prior_references=state.get("active_references", []),
                project_rule_files=event.get("project_rule_files", []),
            )
            state["active_skills"] = selection["skills"]
            state["active_references"] = selection["references"]
            state["rules_hash"] = selection["rules_hash"]
            holder["selection"] = selection
            agent_id = event.get("agent_id")
            agent_loaded_hash = (
                state.get("agents", {}).get(agent_id, {}).get("loaded_rules_hash")
                if agent_id
                else state.get("loaded_rules_hash")
            )
            holder["load_required"] = agent_loaded_hash != selection["loaded_rules_hash"]

        # 检查结果同时绑定代码 diff 和规则版本，任一变化都会触发重新检查。
        current_diff_hash = calculate_task_diff_hash(changes, selection["rules_hash"])
        hashes = file_hash_map(changes)
        check = state["check"]
        if (
            current_diff_hash == check.get("diff_hash")
            and check.get("tool_status") == "passed"
            and check.get("semantic_status") == "passed"
            and not event.get("check_result")
        ):
            holder["status"] = "passed"
            holder["finish_response"] = {
                "result": "passed",
                "current_diff_hash": current_diff_hash,
                "changes": [change["path"] for change in changes],
                "contains_existing_changes": workspace["contains_existing_changes"],
                "semantic_checklist": [],
                "task_report": {
                    "changed_file_count": len(changes),
                    "contains_existing_changes": workspace["contains_existing_changes"],
                    "result": "passed",
                },
            }
            return

        check_result = event.get("check_result")
        checklist = build_semantic_checklist(selection["loaded_files"])
        if check_result is None:
            self._request_check(
                state,
                current_diff_hash,
                hashes,
                changes,
                workspace["contains_existing_changes"],
                checklist,
                holder,
                reason="invalid_check_result" if event.get("check_result_error") else None,
            )
            return

        tool_status = check_result.get("tool_status")
        semantic_status = check_result.get("semantic_status")
        supplied_hash = check_result.get("diff_hash")
        findings = check_result.get("findings", [])
        valid_findings = (
            isinstance(findings, list)
            and all(isinstance(finding, dict) for finding in findings)
            and all(finding.get("result") in RESULTS - {"passed"} for finding in findings)
        )
        if (
            tool_status not in {"passed", "failed"}
            or semantic_status not in {"passed", "failed"}
            or not isinstance(supplied_hash, str)
            or not supplied_hash
            or not valid_findings
            or (findings and tool_status == "passed" and semantic_status == "passed")
        ):
            self._request_check(
                state,
                current_diff_hash,
                hashes,
                changes,
                workspace["contains_existing_changes"],
                checklist,
                holder,
                reason="invalid_check_result",
            )
            return
        if supplied_hash != current_diff_hash:
            self._request_check(
                state,
                current_diff_hash,
                hashes,
                changes,
                workspace["contains_existing_changes"],
                checklist,
                holder,
                reason="stale_check_result",
            )
            return

        continuation_count = state["check"].get("continuation_count", 0)
        state["check"] = {
            "diff_hash": current_diff_hash,
            "file_hashes": hashes,
            "tool_status": tool_status,
            "semantic_status": semantic_status,
            "continuation_count": continuation_count,
        }
        if tool_status == "failed" and not findings:
            findings = [{"result": "check_failed", "issue_type": "tool_check"}]
        if semantic_status == "failed" and not findings:
            findings = [{"result": "check_failed", "issue_type": "semantic_check"}]
        # observe 只记录 would_repair；repair 才允许在循环保护范围内继续自动修复。
        decision = evaluate_findings(state, findings, current_diff_hash, mode)
        if decision["result"] == "passed" and (tool_status != "passed" or semantic_status != "passed"):
            decision = {
                "result": "check_failed",
                "would_repair": False,
                "repair_allowed": False,
                "reason": "incomplete_check",
            }
        if decision["result"] == "passed":
            state["check"]["continuation_count"] = 0
        holder["status"] = decision["result"]
        holder["finish_response"] = {
            **decision,
            "current_diff_hash": current_diff_hash,
            "changes": [change["path"] for change in changes],
            "contains_existing_changes": workspace["contains_existing_changes"],
            "semantic_checklist": [] if decision["result"] == "passed" else checklist,
            "findings": findings,
            "repair_suggestions": [
                {
                    "rule_id": finding.get("rule_id"),
                    "file": finding.get("file"),
                    "action": finding.get("suggestion"),
                }
                for finding in findings
                if finding.get("result") == "repairable"
            ],
            "user_questions": [
                {
                    "rule_id": finding.get("rule_id"),
                    "question": finding.get("question"),
                }
                for finding in findings
                if finding.get("result") == "needs_user_decision"
            ],
            "task_report": {
                "changed_file_count": len(changes),
                "contains_existing_changes": workspace["contains_existing_changes"],
                "result": decision["result"],
                "repair_round": state["repair"]["round"],
            },
        }

    def _request_check(
        self,
        state: Dict[str, Any],
        current_diff_hash: str,
        hashes: Dict[str, str],
        changes: List[Dict[str, Any]],
        contains_existing_changes: bool,
        checklist: List[Dict[str, str]],
        holder: Dict[str, Any],
        reason: Optional[str] = None,
    ) -> None:
        """为当前指纹发起有限次数的 Agent 检查续接，避免 pending 无限循环。"""

        previous = state.get("check", {})
        previous_count = (
            previous.get("continuation_count", 0)
            if previous.get("diff_hash") == current_diff_hash
            else 0
        )
        if previous_count >= MAX_CHECK_CONTINUATIONS:
            state["check"] = {
                "diff_hash": current_diff_hash,
                "file_hashes": hashes,
                "tool_status": "failed",
                "semantic_status": "failed",
                "continuation_count": previous_count,
            }
            holder["status"] = "check_failed"
            holder["finish_response"] = {
                "result": "check_failed",
                "reason": "check_continuation_limit",
                "current_diff_hash": current_diff_hash,
                "changes": [change["path"] for change in changes],
                "contains_existing_changes": contains_existing_changes,
                "semantic_checklist": checklist,
                "warnings": ["同一 diff 已连续三次未形成有效检查结果，已停止自动续接。"],
                "task_report": {
                    "changed_file_count": len(changes),
                    "contains_existing_changes": contains_existing_changes,
                    "result": "check_failed",
                },
            }
            return

        continuation_count = previous_count + 1
        state["check"] = {
            "diff_hash": current_diff_hash,
            "file_hashes": hashes,
            "tool_status": "pending",
            "semantic_status": "pending",
            "continuation_count": continuation_count,
        }
        holder["status"] = "check_required"
        holder["finish_response"] = {
            "result": None,
            "reason": reason,
            "current_diff_hash": current_diff_hash,
            "changes": [change["path"] for change in changes],
            "contains_existing_changes": contains_existing_changes,
            "semantic_checklist": checklist,
            "task_report": {
                "changed_file_count": len(changes),
                "contains_existing_changes": contains_existing_changes,
                "result": "check_required",
                "continuation_count": continuation_count,
            },
        }


def main() -> int:
    """通过 stdin/stdout 提供不绑定具体工具的 JSON 命令入口。"""

    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        json.dump({"status": "check_failed", "result": "check_failed", "warnings": [str(error)]}, sys.stdout)
        sys.stdout.write("\n")
        return 0

    if isinstance(event, dict):
        rules_root = Path(event.pop("rules_root")) if event.get("rules_root") else None
        skills_root = Path(event.pop("skills_root")) if event.get("skills_root") else None
        data_home = Path(event.pop("data_home")) if event.get("data_home") else None
    else:
        rules_root = None
        skills_root = None
        data_home = None
    core = RulesLoopCore(rules_root=rules_root, skills_root=skills_root, data_home=data_home)
    json.dump(core.process(event), sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
