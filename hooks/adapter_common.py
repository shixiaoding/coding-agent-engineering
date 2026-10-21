#!/usr/bin/env python3
"""Codex、Qoder 等工具 Adapter 共享的轻量输入分类能力。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional

from workspace import is_implementation_path


COMMAND_FILE_PATTERN = re.compile(
    r"(?<![\w.-])((?:/|\.{1,2}/)?[\w@+.,/-]+\.(?:css|go|gradle|groovy|html|java|js|json|kt|kts|php|properties|py|rb|scss|sh|sql|ts|tsx|vue|xml|yaml|yml))"
)
CODING_INTENT_PATTERN = re.compile(
    r"(?:实现|修改|修复|开发|编码|重构|补(?:充)?测试|新增接口|写代码|implement|modify|fix|refactor|add tests?|new endpoint)",
    re.IGNORECASE,
)
STRONG_CODING_INTENT_PATTERN = re.compile(
    r"(?:实现|修复|开发|编码|重构|补(?:充)?测试|新增接口|写代码|(?:并|然后|直接|请)修改|"
    r"implement|fix|refactor|add tests?|new endpoint)",
    re.IGNORECASE,
)
READ_ONLY_INTENT_PATTERN = re.compile(r"(?:分析|解释|评审|审查|看看|查看|读取|查询|review|explain|analy[sz]e)", re.IGNORECASE)
NO_CODING_INTENT_PATTERN = re.compile(
    r"(?:不要|无需|不需要|先不|暂不)(?:修改|实现|写代码|改代码)|(?:只|仅)(?:分析|解释|评审|审查|查看)|"
    r"(?:do not|don't) (?:modify|implement|change code)",
    re.IGNORECASE,
)
USER_DECISION_PATTERN = re.compile(
    r"(?:确认|同意|不同意|就按|保留|不要改|改成|采用|不采用)|"
    r"(?:^|[，。,\s])可以(?=$|[，。,\s]|按|这样|处理|修改)|"
    r"\b(?:yes|no|agree|keep)\b|change to",
    re.IGNORECASE,
)
CHANGE_HINT_PATTERNS = {
    "new_endpoint": r"新增接口|新接口|new endpoint|\*\*\* add file: .*controller",
    "contract_change": r"契约变更|兼容(?:性)?|contract change",
    "request_change": r"请求字段|request field|\*\*\* (?:add|update) file: .*request\.java",
    "response_change": r"响应字段|response field|\*\*\* (?:add|update) file: .*response\.java",
    "transaction_change": r"事务|@transactional|transaction",
    "concurrency_change": r"并发|线程池|锁|concurren|thread pool",
    "performance_change": r"性能|慢查询|performance|slow query",
    "test_strategy_change": r"测试策略|集成测试|test strategy|integration test",
    "version_change": r"java\s*(?:版本|version)|spring\s*boot\s*(?:版本|version)",
    "layering_change": r"分层|领域层|架构调整|layering|architecture",
}
WRITE_COMMAND_PATTERN = re.compile(
    r"(?:^|[\s;&|])(?:cp|install|mkdir|mv|rm|sed\s+-i|tee|touch|truncate)\b|(?:^|\s)(?:>|>>)|"
    r"\b(?:spotless:apply|format|formatter|git\s+apply)\b",
    re.IGNORECASE,
)
WRITE_WITHOUT_PATH_PATTERN = re.compile(
    r"\b(?:spotless:apply|format|formatter|git\s+apply|generate-sources|generate-resources)\b",
    re.IGNORECASE,
)


def normalize_path(value: str, cwd: Path, repository_root: Optional[Path]) -> Optional[str]:
    if not value or repository_root is None:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    candidate = candidate.resolve()
    try:
        relative = candidate.relative_to(repository_root)
    except ValueError:
        return None
    normalized = str(relative)
    return normalized if is_implementation_path(normalized) else None


def path_values(value: Any, key: Optional[str] = None) -> Iterable[str]:
    """只从明确表示路径的工具字段递归提取值，避免把普通输出当成文件名。"""

    path_keys = {"file", "file_path", "filepath", "path", "paths", "files", "target", "destination"}
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            yield from path_values(child_value, child_key.lower())
    elif isinstance(value, list):
        for item in value:
            yield from path_values(item, key)
    elif isinstance(value, str) and key in path_keys:
        yield value


def command_file_values(command: str) -> Iterable[str]:
    return COMMAND_FILE_PATTERN.findall(command)


def is_write_command(command: str, has_files: bool) -> bool:
    pattern = WRITE_COMMAND_PATTERN if has_files else WRITE_WITHOUT_PATH_PATTERN
    return bool(pattern.search(command))


def classify_coding_intent(prompt: str) -> bool:
    if NO_CODING_INTENT_PATTERN.search(prompt):
        return False
    # 文档需求不预热编码规则；混合任务由后续实际代码写入兜底激活。
    if re.search(r"文档|文章|笔记|说明书|README|\.md\b|\.txt\b|\.docx\b", prompt, re.IGNORECASE):
        return False
    if not CODING_INTENT_PATTERN.search(prompt):
        return False
    if READ_ONLY_INTENT_PATTERN.search(prompt) and not STRONG_CODING_INTENT_PATTERN.search(prompt):
        return False
    return True


def classify_user_decision(prompt: str) -> bool:
    return bool(USER_DECISION_PATTERN.search(prompt))


def classify_change_hints(text_parts: Iterable[str], files: Iterable[str]) -> list[str]:
    text = "\n".join(text_parts).lower()
    hints = {
        hint
        for hint, pattern in CHANGE_HINT_PATTERNS.items()
        if re.search(pattern, text, re.IGNORECASE)
    }
    if any(Path(path).name.lower().startswith(("openapi", "swagger")) for path in files):
        hints.add("openapi_change")
    return sorted(hints)
