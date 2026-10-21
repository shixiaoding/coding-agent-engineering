#!/usr/bin/env python3
"""持久化会话状态并控制自动修复循环。

状态只保存恢复和防循环所需的结构化摘要，不保存源码、完整 diff、用户问题正文
或修复建议正文。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


SCHEMA_VERSION = 1
RESULTS = {"passed", "repairable", "needs_user_decision", "check_failed"}


class StateError(RuntimeError):
    """状态无法安全持久化时抛出。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_check() -> Dict[str, Any]:
    """创建与某个任务总指纹绑定的检查状态。"""

    return {
        "diff_hash": None,
        "file_hashes": {},
        "tool_status": "not_started",
        "semantic_status": "not_started",
        "continuation_count": 0,
    }


def default_repair() -> Dict[str, Any]:
    """创建一条新的自动修复链路及其循环保护计数。"""

    return {
        "round": 0,
        "seen_diff_hashes": [],
        "last_issue_hash": None,
        "same_issue_count": 0,
        "waiting_for_user": False,
        "pending_issue_hash": None,
    }


def new_state(repository_root: str, session_id: str) -> Dict[str, Any]:
    """创建会话级状态；Common 始终生效，因此只记录专业 Skill。"""

    return {
        "schema_version": SCHEMA_VERSION,
        "repository_root": str(Path(repository_root).resolve()),
        "session_id": session_id,
        "coding_active": False,
        "active_skills": [],
        "active_references": [],
        "change_hints": [],
        "rules_hash": None,
        "loaded_rules_hash": None,
        "baseline": None,
        "touched_files": [],
        "mode_override": None,
        "check": default_check(),
        "repair": default_repair(),
        "current_turn": {"turn_id": None, "prewrite_active": False},
        "agents": {},
        "last_updated_at": utc_now(),
    }


def invalidate_check(state: Dict[str, Any]) -> None:
    state["check"] = default_check()


def reset_repair(state: Dict[str, Any]) -> None:
    state["repair"] = default_repair()


def _merge_unique(existing: Iterable[str], added: Iterable[str]) -> List[str]:
    return sorted(set(existing) | {value for value in added if value})


def transition_state(state: Dict[str, Any], event_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """将统一事件应用到状态副本，避免调用方意外修改旧状态。"""

    updated = copy.deepcopy(state)
    turn_id = payload.get("turn_id")
    agent_id = payload.get("agent_id")

    if event_name == "session_initialized":
        updated["current_turn"] = {
            "turn_id": turn_id,
            "prewrite_active": False,
            "coding_active": updated["current_turn"].get("coding_active", updated["coding_active"]),
        }
    elif event_name == "context_restored":
        updated["current_turn"] = {
            "turn_id": turn_id,
            "prewrite_active": False,
            "coding_active": updated["current_turn"].get("coding_active", updated["coding_active"]),
        }
    elif event_name == "session_ended":
        pass
    elif event_name == "task_intent_received":
        updated["current_turn"]["turn_id"] = turn_id
        # 保留任务基线供后续代码续接，但只读或文档回合不触发编码检查。
        updated["current_turn"]["coding_active"] = bool(payload.get("coding_intent"))
        if payload.get("coding_intent"):
            updated["coding_active"] = True
            if payload.get("baseline") is not None and updated.get("baseline") is None:
                updated["baseline"] = payload["baseline"]
        if payload.get("user_decision_received") and updated["repair"]["waiting_for_user"]:
            reset_repair(updated)
            invalidate_check(updated)
    elif event_name == "agent_started":
        if agent_id:
            updated["agents"].setdefault(
                agent_id,
                {
                    "touched_files": [],
                    "active_skills": [],
                    "loaded_rules_hash": None,
                    "last_updated_at": utc_now(),
                },
            )
    elif event_name == "before_code_write":
        updated["coding_active"] = True
        updated["current_turn"] = {"turn_id": turn_id, "prewrite_active": True, "coding_active": True}
        if payload.get("baseline") is not None and updated.get("baseline") is None:
            updated["baseline"] = payload["baseline"]
    elif event_name == "after_code_write":
        updated["coding_active"] = True
        updated["current_turn"] = {"turn_id": turn_id, "prewrite_active": False, "coding_active": True}
        files = payload.get("files", [])
        skills = payload.get("active_skills", [])
        references = payload.get("active_references", [])
        hints = payload.get("change_hints", [])
        updated["touched_files"] = _merge_unique(updated["touched_files"], files)
        updated["active_skills"] = _merge_unique(updated["active_skills"], skills)
        updated["active_references"] = _merge_unique(updated["active_references"], references)
        updated["change_hints"] = _merge_unique(updated["change_hints"], hints)
        invalidate_check(updated)
        if agent_id:
            agent = updated["agents"].setdefault(
                agent_id,
                {
                    "touched_files": [],
                    "active_skills": [],
                    "loaded_rules_hash": None,
                    "last_updated_at": utc_now(),
                },
            )
            agent["touched_files"] = _merge_unique(agent["touched_files"], files)
            agent["active_skills"] = _merge_unique(agent["active_skills"], skills)
            agent["last_updated_at"] = utc_now()
    elif event_name == "before_task_finish":
        updated["current_turn"]["turn_id"] = turn_id
    elif event_name == "turn_interrupted":
        updated["current_turn"] = {
            "turn_id": turn_id,
            "prewrite_active": False,
            "coding_active": updated["current_turn"].get("coding_active", updated["coding_active"]),
        }
    else:
        raise StateError(f"未知统一事件：{event_name}")

    updated["last_updated_at"] = utc_now()
    return updated


def issue_hash(findings: Iterable[Dict[str, Any]]) -> str:
    """只使用问题类型和位置生成摘要，不把问题正文持久化。"""

    stable_items = sorted(
        (
            finding.get("result"),
            finding.get("rule_id"),
            finding.get("issue_type"),
            finding.get("file"),
        )
        for finding in findings
    )
    encoded = json.dumps(stable_items, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_findings(
    state: Dict[str, Any],
    findings: List[Dict[str, Any]],
    current_diff_hash: str,
    mode: str,
) -> Dict[str, Any]:
    """分类检查结果，并在 repair 模式下执行轮次与循环保护。"""

    if mode not in {"observe", "repair"}:
        raise StateError(f"未知运行模式：{mode}")
    for finding in findings:
        if finding.get("result") not in RESULTS - {"passed"}:
            raise StateError(f"非法检查结果：{finding.get('result')}")

    if not findings:
        reset_repair(state)
        return {"result": "passed", "would_repair": False, "repair_allowed": False, "reason": None}

    # 1、检查器故障和业务歧义优先返回，不进入自动修复。
    if any(finding["result"] == "check_failed" for finding in findings):
        return {"result": "check_failed", "would_repair": False, "repair_allowed": False, "reason": "checker_failure"}

    current_issue_hash = issue_hash(findings)
    repair = state["repair"]
    if any(finding["result"] == "needs_user_decision" for finding in findings):
        repair["waiting_for_user"] = True
        repair["pending_issue_hash"] = current_issue_hash
        return {
            "result": "needs_user_decision",
            "would_repair": False,
            "repair_allowed": False,
            "reason": "ambiguous_issue",
        }

    if mode == "observe":
        return {"result": "repairable", "would_repair": True, "repair_allowed": False, "reason": "observe_mode"}

    # 2、先识别无变化和历史指纹，防止 Agent 在多个结果之间反复改写。
    if repair["round"] > 0 and repair["seen_diff_hashes"][-1] == current_diff_hash:
        repair["waiting_for_user"] = True
        repair["pending_issue_hash"] = current_issue_hash
        return {
            "result": "needs_user_decision",
            "would_repair": False,
            "repair_allowed": False,
            "reason": "diff_unchanged_after_repair",
        }

    if current_diff_hash in repair["seen_diff_hashes"]:
        repair["waiting_for_user"] = True
        repair["pending_issue_hash"] = current_issue_hash
        return {
            "result": "needs_user_decision",
            "would_repair": False,
            "repair_allowed": False,
            "reason": "diff_cycle",
        }

    # 3、同一问题连续出现或达到轮次上限时，把剩余选择交给用户。
    if repair["last_issue_hash"] == current_issue_hash:
        repair["same_issue_count"] += 1
    else:
        repair["last_issue_hash"] = current_issue_hash
        repair["same_issue_count"] = 1

    if repair["same_issue_count"] >= 2:
        repair["waiting_for_user"] = True
        repair["pending_issue_hash"] = current_issue_hash
        return {
            "result": "needs_user_decision",
            "would_repair": False,
            "repair_allowed": False,
            "reason": "same_issue_repeated",
        }

    if repair["round"] >= 3:
        repair["waiting_for_user"] = True
        repair["pending_issue_hash"] = current_issue_hash
        return {
            "result": "needs_user_decision",
            "would_repair": False,
            "repair_allowed": False,
            "reason": "repair_limit_reached",
        }

    repair["round"] += 1
    repair["seen_diff_hashes"].append(current_diff_hash)
    repair["waiting_for_user"] = False
    repair["pending_issue_hash"] = None
    return {"result": "repairable", "would_repair": True, "repair_allowed": True, "reason": None}


class StateStore:
    """在工具无关的用户数据目录中原子读写会话状态。"""

    def __init__(self, data_home: Optional[Path] = None, lock_timeout_seconds: float = 5.0):
        configured_home = os.environ.get("CODING_RULES_LOOP_HOME")
        self.data_home = Path(data_home or configured_home or (Path.home() / ".coding-rules-loop")).expanduser()
        self.state_root = self.data_home / "state"
        self.lock_timeout_seconds = lock_timeout_seconds

    def _key(self, repository_root: str, session_id: str) -> Tuple[str, str]:
        """使用摘要作为目录名，避免路径和会话 ID 直接暴露在文件名中。"""

        repository_key = hashlib.sha256(str(Path(repository_root).resolve()).encode("utf-8")).hexdigest()[:20]
        session_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:20]
        return repository_key, session_key

    def state_path(self, repository_root: str, session_id: str) -> Path:
        repository_key, session_key = self._key(repository_root, session_id)
        return self.state_root / repository_key / f"{session_key}.json"

    @contextmanager
    def _lock(self, state_path: Path):
        """使用独占锁文件串行化同一会话的主 Agent 与子 Agent 更新。"""

        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = state_path.with_suffix(".lock")
        deadline = time.monotonic() + self.lock_timeout_seconds
        descriptor: Optional[int] = None
        while descriptor is None:
            try:
                descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(descriptor, str(os.getpid()).encode("ascii"))
            except FileExistsError:
                try:
                    stale = time.time() - lock_path.stat().st_mtime > max(30.0, self.lock_timeout_seconds * 2)
                    if stale:
                        lock_path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise StateError(f"状态锁等待超时：{lock_path}")
                time.sleep(0.01)
        try:
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def _load_unlocked(self, repository_root: str, session_id: str) -> Tuple[Dict[str, Any], List[str]]:
        """读取并校验状态；损坏文件先隔离，再以空状态 fail-open。"""

        path = self.state_path(repository_root, session_id)
        if not path.exists():
            return new_state(repository_root, session_id), []
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            corrupt_path = path.with_suffix(f".corrupt-{suffix}.json")
            os.replace(path, corrupt_path)
            return new_state(repository_root, session_id), [f"状态损坏，已隔离为 {corrupt_path.name}：{error}"]
        if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
            return new_state(repository_root, session_id), ["状态版本不兼容，已使用空状态恢复"]
        required = {
            "repository_root",
            "session_id",
            "coding_active",
            "active_skills",
            "active_references",
            "change_hints",
            "baseline",
            "touched_files",
            "check",
            "repair",
            "current_turn",
            "agents",
        }
        if not required.issubset(state):
            suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            corrupt_path = path.with_suffix(f".corrupt-{suffix}.json")
            os.replace(path, corrupt_path)
            return new_state(repository_root, session_id), [f"状态字段不完整，已隔离为 {corrupt_path.name}"]
        return state, []

    def load(self, repository_root: str, session_id: str) -> Tuple[Dict[str, Any], List[str]]:
        path = self.state_path(repository_root, session_id)
        with self._lock(path):
            return self._load_unlocked(repository_root, session_id)

    def _save_unlocked(self, state: Dict[str, Any]) -> None:
        """先写临时文件并 fsync，再原子替换正式状态文件。"""

        path = self.state_path(state["repository_root"], state["session_id"])
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        state["last_updated_at"] = utc_now()
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            json.dump(state, temporary, ensure_ascii=False, sort_keys=True, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)

    def update(
        self,
        repository_root: str,
        session_id: str,
        updater: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], List[str]]:
        path = self.state_path(repository_root, session_id)
        with self._lock(path):
            state, warnings = self._load_unlocked(repository_root, session_id)
            updated = updater(state)
            self._save_unlocked(updated)
            return updated, warnings

    def clear(self, repository_root: str, session_id: str) -> None:
        path = self.state_path(repository_root, session_id)
        with self._lock(path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def cleanup(self, inactive_days: int = 30, now: Optional[datetime] = None) -> int:
        """启动时惰性删除长期未活动的状态，并用逐文件锁避开并发写入。"""

        if not self.state_root.exists():
            return 0
        threshold = (now or datetime.now(timezone.utc)) - timedelta(days=inactive_days)
        removed = 0
        for path in self.state_root.glob("*/*.json"):
            with self._lock(path):
                if not path.exists():
                    continue
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                if modified < threshold:
                    path.unlink()
                    removed += 1
        return removed
