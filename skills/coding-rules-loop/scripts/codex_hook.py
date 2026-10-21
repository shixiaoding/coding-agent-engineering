#!/usr/bin/env python3
"""从全局 Skill 软链接定位源码仓库，并启动 Codex Adapter。"""

import os
import sys
from pathlib import Path


def main() -> None:
    source_root = Path(__file__).resolve().parents[3]
    adapter = source_root / "hooks" / "codex" / "coding_rules_loop_hook.py"
    os.execv(sys.executable, [sys.executable, str(adapter)])


if __name__ == "__main__":
    main()
