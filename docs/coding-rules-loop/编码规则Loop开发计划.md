# 编码规则 Loop 开发计划

## 元信息

- 版本：v0.9 通用副本
- 日期：2026-10-21
- 状态：本地复制与脱敏验证完成；真实工具验收按阶段记录
- 关联方案：[编码规则Loop技术方案.md](编码规则Loop技术方案.md)

## 1. 当前交付范围

保留 Common 的规则、案例和学习候选，Java/API Skill 的核心与详细规范，公共 Core，Codex/Qoder IDE Adapter，以及回归测试。API 路径和 HTTP 方法根据目标项目契约决定，不要求所有项目采用同一种风格。

不包含私有服务工具、内部域名、个人安装路径、来源审计、历史对话、实机运行状态或私有 Git 历史。本次只整理本地副本，不执行提交、推送、发布或全局 Hook 安装。

## 2. 阶段与验收

| 阶段 | 目标 | 当前进度与下一项 |
| --- | --- | --- |
| 0 | 方案和职责 | 保留工具无关 Core、独立 Skill、批量 diff 与有限循环 |
| 1 | 规则与专业 Skill | Common、Learning、Java/API 和详细案例已复制；验证引用和通用边界 |
| 2 | 公共 Core | 规则选择、基线、指纹、状态、结果分类与循环保护已复制；运行本地测试 |
| 3 | Codex Adapter | 检查续接和 JSON 回写已实现；验证本副本包装入口 |
| 4 | 用户级接入 | 待用户选择本副本作为安装源后，备份并合并配置、核对信任状态 |
| 5 | Codex 真实任务 | 在本副本对应环境完成 1～3 个真实任务，包含一次受控 repair |
| 6 | Qoder IDE | Adapter 与 fixture 已复制；补齐实机检查回写与正负向烟测 |
| 7 | Claude | 待核对协议并实现 Adapter |
| 8 | 跨工具验证 | 累计 5～10 个任务，记录误判、漏判和检查结果 |
| 9 | 规则与 Learning | 根据真实证据校准，人工审核候选 |
| 10 | 逐工具启用 repair | 验证明确问题修复、歧义咨询和回滚 |
| 11 | 插件化 | 稳定后再处理安装、升级、卸载和用户数据隔离 |
| 12 | 公开发布准备 | 独立检查授权、目标仓库历史、敏感信息和发行内容 |

代码复制不改变真实验收状态。已有 Core 测试或其他环境的运行经验，不直接作为本副本的全局安装和真实任务验收记录。

## 3. 本地验证

在仓库根目录执行。测试使用临时 Git 仓库和临时数据目录，不应安装全局 Hook：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/core -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/hooks -p 'test_codex_hook.py' -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/hooks -p 'test_qoder_hook.py' -v
ruby scripts/validate_rules.rb
ruby tests/rules/test_validate_rules.rb
```

Python 使用标准库；规则校验使用 Ruby 标准库。专业 Skill 的测试案例保留在 `tests/fixtures/rule-cases/`，用于后续回归，不作为日常编码时的默认加载资产。

本地性能目标：规则注入和写入事件 P95 小于 100ms，Stop 本地处理 P95 小于 1s；不包含模型续接和项目测试耗时。

2026-10-21 在通用副本根目录完成验证：Core 36 项、Codex Adapter 16 项、Qoder Adapter 11 项 Python 测试通过；规则测试 11 组、40 个断言通过；30 条正式规则与 2 条 Learning 记录校验通过；技术方案与新增文档的本地链接校验通过。Codex Stop P95 约 52ms，Qoder Stop P95 约 58ms。上述结果属于本地测试，不表示新的全局安装或实机验收已完成。

## 4. 实机步骤

1. 选择完整仓库路径作为安装源，备份已有规则、Skill 和 Hook。
2. 分别链接规则和三个 Skill，合并对应工具的 Hook 模板。
3. 检查 Skill 发现和 Hook 信任；Qoder IDE 配置变更后重启。
4. 用只读和文档任务确认跳过，再用真实 Java/API 任务验证。
5. 观察 Stop 首次 pending、结构化回写、指纹匹配和检查通过。
6. Codex 在单独受控环境切到 repair，验证修复、新 diff 复查和三轮保护。
7. Qoder 实机 payload 与 fixture 不一致时，先调整其 Adapter 和用例；不预设其拥有 Codex 的续接机制。

真实任务记录规则选择、Feign 边界、验证命令、检查结果、修复轮次和未解决歧义。没有有效结果回写时不能记录为语义检查通过。

## 5. 后续同步与回滚

通用副本保持自己的 Git 分支和历史。后续同步时先比较 Core/Adapter 的差异，再处理规范与文档，不整体覆盖目标仓库。

复制新增文件可在 Review 后单独保留或撤回；README 的本次修改可按 diff 回退。已有协作规则、代码审查 Skill、未提交文档及本机配置不属于本次替换范围。正式安装后的回滚应恢复安装前的配置备份，并保留用户数据。
