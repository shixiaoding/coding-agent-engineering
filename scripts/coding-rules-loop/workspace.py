#!/usr/bin/env python3
"""生成 Git 工作区基线和稳定的任务 diff 指纹。

本模块只读取 Git 和文件内容摘要，不修改、暂存或回退业务仓库文件。baseline
用于区分任务开始前的用户改动与本轮新增变化。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


IMPLEMENTATION_SUFFIXES = {
    ".css",
    ".go",
    ".gradle",
    ".groovy",
    ".html",
    ".java",
    ".js",
    ".json",
    ".kt",
    ".kts",
    ".php",
    ".properties",
    ".py",
    ".rb",
    ".scss",
    ".sh",
    ".sql",
    ".ts",
    ".tsx",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}


def is_implementation_path(path: str) -> bool:
    candidate = Path(path)
    if candidate.name in {"SKILL.md", "AGENTS.md"}:
        return True
    if candidate.name in {"coding-rules.md", "coding-rules-learning.md"}:
        return True
    return candidate.suffix.lower() in IMPLEMENTATION_SUFFIXES


class WorkspaceError(RuntimeError):
    """工作区无法安全读取时抛出。"""


class NotGitRepository(WorkspaceError):
    """当前目录不属于 Git 仓库，属于正常跳过而非检查故障。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_git(repository_root: Path, arguments: Sequence[str], check: bool = True) -> bytes:
    process = subprocess.run(
        ["git", "-C", str(repository_root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    if check and process.returncode != 0:
        message = process.stderr.decode("utf-8", "replace").strip()
        # 存在 .git 入口却无法识别时可能是仓库损坏，不能当普通目录跳过。
        has_git_marker = any((parent / ".git").exists() for parent in (repository_root, *repository_root.parents))
        if "not a git repository" in message and not has_git_marker:
            raise NotGitRepository(message)
        raise WorkspaceError(message or f"git {' '.join(arguments)} failed")
    return process.stdout


def find_repository_root(path: str) -> Path:
    candidate = Path(path).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    output = _run_git(candidate, ["rev-parse", "--show-toplevel"])
    return Path(output.decode("utf-8", "surrogateescape").strip()).resolve()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(encoded)


def _content_metadata(path: Path) -> Dict[str, Any]:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return {"content_hash": None, "mode": None}

    mode = stat.S_IMODE(file_stat.st_mode)
    if path.is_symlink():
        content = b"symlink\0" + os.readlink(path).encode("utf-8", "surrogateescape")
    elif path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"content_hash": digest.hexdigest(), "mode": mode}
    else:
        content = f"special:{file_stat.st_mode}".encode("ascii")
    return {"content_hash": _sha256_bytes(content), "mode": mode}


def _parse_porcelain_v2(raw: bytes) -> List[Dict[str, Any]]:
    """解析适合机器读取的 Git porcelain v2，包括空格路径和重命名。"""

    tokens = raw.split(b"\0")
    entries: List[Dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        record = token.decode("utf-8", "surrogateescape")
        prefix = record[:2]
        if prefix == "1 ":
            parts = record.split(" ", 8)
            if len(parts) != 9:
                raise WorkspaceError(f"无法解析 Git 状态记录：{record}")
            entries.append({"path": parts[8], "status": parts[1], "original_path": None, "tracked": True})
        elif prefix == "2 ":
            parts = record.split(" ", 9)
            if len(parts) != 10 or index >= len(tokens):
                raise WorkspaceError(f"无法解析 Git 重命名记录：{record}")
            original_path = tokens[index].decode("utf-8", "surrogateescape")
            index += 1
            entries.append(
                {"path": parts[9], "status": parts[1], "original_path": original_path, "tracked": True}
            )
        elif prefix == "u ":
            parts = record.split(" ", 10)
            if len(parts) != 11:
                raise WorkspaceError(f"无法解析 Git 冲突记录：{record}")
            entries.append({"path": parts[10], "status": "UU", "original_path": None, "tracked": True})
        elif prefix == "? ":
            entries.append({"path": record[2:], "status": "??", "original_path": None, "tracked": False})
        elif prefix == "! ":
            continue
        else:
            raise WorkspaceError(f"未知 Git 状态记录：{record}")
    return entries


def _status_entries(repository_root: Path) -> List[Dict[str, Any]]:
    raw = _run_git(
        repository_root,
        ["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignore-submodules=none", "--renames"],
    )
    return _parse_porcelain_v2(raw)


def _head_revision(repository_root: Path) -> Optional[str]:
    process = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "--verify", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        return None
    return process.stdout.decode("ascii", "replace").strip()


def _tracked_diff_hash(
    repository_root: Path,
    path: str,
    original_path: Optional[str],
    base_revision: str = "HEAD",
) -> str:
    """计算文件相对指定基线提交的二进制 diff 摘要。"""

    paths = [path]
    if original_path and original_path != path:
        paths.append(original_path)
    diff = _run_git(
        repository_root,
        ["diff", "--binary", "--no-ext-diff", "--find-renames", base_revision, "--", *paths],
    )
    return _sha256_bytes(diff)


def _snapshot_entry(
    repository_root: Path,
    entry: Dict[str, Any],
    base_revision: str = "HEAD",
) -> Dict[str, Any]:
    path = entry["path"]
    metadata = _content_metadata(repository_root / path)
    result = {
        "path": path,
        "status": entry["status"],
        "original_path": entry.get("original_path"),
        "tracked": bool(entry.get("tracked")),
        **metadata,
    }
    if result["tracked"]:
        result["diff_hash"] = _tracked_diff_hash(repository_root, path, result["original_path"], base_revision)
    else:
        result["diff_hash"] = None
    result["file_hash"] = _stable_hash(result)
    return result


def snapshot_workspace(repository_root: str) -> Dict[str, Any]:
    """保存任务开始时的 HEAD 以及已有脏文件和未跟踪文件摘要。"""

    root = find_repository_root(repository_root)
    files = {
        entry["path"]: _snapshot_entry(root, entry)
        for entry in _status_entries(root)
    }
    return {
        "repository_root": str(root),
        "head": _head_revision(root),
        "files": dict(sorted(files.items())),
        "captured_at": utc_now(),
    }


def _entry_signature(entry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if entry is None:
        return None
    return {
        "status": entry.get("status"),
        "original_path": entry.get("original_path"),
        "tracked": entry.get("tracked"),
        "content_hash": entry.get("content_hash"),
        "mode": entry.get("mode"),
        "diff_hash": entry.get("diff_hash"),
    }


def _clean_entry(repository_root: Path, path: str) -> Dict[str, Any]:
    result = {
        "path": path,
        "status": "clean",
        "original_path": None,
        "tracked": True,
        **_content_metadata(repository_root / path),
        "diff_hash": _sha256_bytes(b""),
    }
    result["file_hash"] = _stable_hash(result)
    return result


def _baseline_diff_entries(repository_root: Path, base_revision: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """读取相对 baseline HEAD 的变化，使任务中途 commit 后仍能完成收尾检查。"""

    if not base_revision:
        return {}
    raw = _run_git(
        repository_root,
        ["diff", "--name-status", "-z", "--find-renames", base_revision, "--"],
    )
    tokens = raw.split(b"\0")
    entries: Dict[str, Dict[str, Any]] = {}
    index = 0
    while index < len(tokens):
        status_token = tokens[index]
        index += 1
        if not status_token:
            continue
        status = status_token.decode("ascii", "replace")
        if index >= len(tokens):
            raise WorkspaceError("无法解析相对 baseline HEAD 的文件状态")
        first_path = tokens[index].decode("utf-8", "surrogateescape")
        index += 1
        if status.startswith(("R", "C")):
            if index >= len(tokens):
                raise WorkspaceError("无法解析相对 baseline HEAD 的重命名状态")
            path = tokens[index].decode("utf-8", "surrogateescape")
            index += 1
            original_path = first_path
        else:
            path = first_path
            original_path = None
        entries[path] = {
            "path": path,
            "status": f"baseline:{status}",
            "original_path": original_path,
            "tracked": True,
        }
    return entries


def collect_changes(
    repository_root: str,
    baseline: Dict[str, Any],
    touched_files: Iterable[str] = (),
) -> Dict[str, Any]:
    """合并工具触达、任务后新增和相对 baseline 变化的整批相关文件。"""

    root = find_repository_root(repository_root)
    if Path(baseline.get("repository_root", "")).resolve() != root:
        raise WorkspaceError("baseline 不属于当前仓库")

    # 1、同时读取当前工作区状态和相对任务起点 HEAD 的全部已跟踪变化。
    baseline_files = baseline.get("files", {})
    baseline_head = baseline.get("head")
    base_revision = baseline_head or "HEAD"
    current_files = {
        entry["path"]: _snapshot_entry(root, entry, base_revision)
        for entry in _status_entries(root)
    }
    for path, entry in _baseline_diff_entries(root, baseline_head).items():
        current_files.setdefault(path, _snapshot_entry(root, entry, base_revision))
    # 2、已有脏文件只有被触达或内容再次变化时才进入本轮相关文件集合。
    touched = {str(Path(path)) for path in touched_files if path}
    related_paths = set(current_files) - set(baseline_files)

    for path in set(baseline_files) | set(current_files):
        if _entry_signature(baseline_files.get(path)) != _entry_signature(current_files.get(path)):
            related_paths.add(path)

    related_paths.update(path for path in touched if path in current_files or path in baseline_files)

    # 3、按路径稳定排序并标记既有修改，供上层统一计算任务总指纹。
    changes: List[Dict[str, Any]] = []
    for path in sorted(related_paths):
        current = current_files.get(path)
        if current is None:
            current = _clean_entry(root, path)
        item = dict(current)
        item["existed_before_task"] = path in baseline_files
        changes.append(item)

    return {
        "repository_root": str(root),
        "head": _head_revision(root),
        "baseline_head": baseline_head,
        "changes": changes,
        "contains_existing_changes": any(change["existed_before_task"] for change in changes),
    }


def calculate_task_diff_hash(changes: Iterable[Dict[str, Any]], rules_hash: str) -> str:
    """将排序后的文件子指纹与规则版本合成为任务级检查凭证。"""

    file_hashes = {
        change["path"]: change["file_hash"]
        for change in changes
    }
    payload = {
        "rules_hash": rules_hash,
        "files": [[path, file_hashes[path]] for path in sorted(file_hashes)],
    }
    return _stable_hash(payload)


def file_hash_map(changes: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    return {
        change["path"]: change["file_hash"]
        for change in sorted(changes, key=lambda item: item["path"])
    }
