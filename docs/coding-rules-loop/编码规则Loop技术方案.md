# 编码规则 Loop 技术方案

## 元信息

- 版本：v0.9 通用副本
- 状态：Codex observe 已实现；受控 repair 与跨工具实机验收待完成
- 方案类型：技术改造
- 影响范围：本地规则资产、工具 Hook、会话状态；无数据库变更
- 日期：2026-10-21

## 1. 背景与范围

编码 Agent 在长任务、上下文压缩和多文件修改后可能遗漏规范，或者在修复后直接结束而没有检查新 diff。Coding Rules Loop 在代码写入前加载适用规则，在收尾时对整批相关变更执行检查，并把明确问题交给当前 Agent 修复。

系统包含当前任务的“加载、检查、修复、复查”和跨任务的“记录、验证、提炼、人工晋升”两个闭环。正式规则由用户维护，不自动替代项目业务约束、测试或人工 Review。

本副本只包含跨项目可复用内容。专业规范保留详细说明和案例，接口风格服从目标项目契约。外部服务配置、历史任务日志和本机安装状态不随代码分发。

## 2. 现状与影响

当前有公共 Core、Codex Adapter、Qoder IDE Adapter、Common 规则、三个 Skill 和本地测试。Codex Stop 可以发出检查续接，并通过候选回复回写结构化结果。Qoder IDE Adapter 已有协议 fixture，仍保留不续接的 observe 行为，等待实机验证后适配检查回写。

Core 的 repair 状态机和 Codex 修复续接已有实现，尚需受控真实任务验收。代码存在和 fixture 通过不能替代工具实机验收。

## 3. 组件与职责

| 组件 | 职责 |
| --- | --- |
| `rules/coding-rules.md` | Common、启用的专业 Skill、稳定偏好、案例和检查清单 |
| `rules/coding-rules-learning.md` | 不参与正式加载的候选 |
| `skills/coding-rules-loop/` | 当前任务与 Learning 的语义执行协议 |
| `skills/coding-standards-java/` | Java 核心规则和按需读取的 `references/java-spec.md` |
| `skills/api-design/` | API 核心规则和按需读取的 `references/api-spec.md` |
| `scripts/coding-rules-loop/core.py` | 规则选择、整批检查编排与结果判定 |
| `scripts/coding-rules-loop/state.py` | 状态持久化、锁和循环保护 |
| `scripts/coding-rules-loop/workspace.py` | Git 基线、变更收集和指纹 |
| `hooks/adapter_common.py` | Adapter 共用的意图、路径和写入分类 |
| `hooks/codex/`、`hooks/qoder/` | 工具事件转换、上下文注入和输出适配 |

Core 不调用模型，也不直接改业务代码。Adapter 传递检查与修复要求；当前编码 Agent 理解规则、运行验证和修改代码。

## 4. 加载与检查流程

1. 非 Git、普通文档和只读回合跳过；损坏或无权限的 Git 仓库属于检查故障。
2. 明确编码意图预热规则；第一次真实写入兜底确认，并保存修改前基线。
3. Common 始终生效，专业 Skill 根据整批文件和变更意图选择。
4. 写入后合并触达文件，让旧检查结果失效。
5. Stop 收集当前相关变更并计算代码与规则的组合指纹。
6. 当前指纹尚未通过时，Codex Adapter 续接 Agent 执行工具和语义检查。
7. Agent 回写结果，Core 验证指纹并决定通过、记录建议、修复或咨询用户。
8. 修复产生新 diff 后再次检查，直到通过或达到保护条件。

普通 Java 修改只读取主 Skill。版本、分层、事务、并发、性能或测试策略变化时读取 Java 详细规范。API 新增或契约变化时读取 API 详细规范；已有接口仅内部实现变化时只读取核心规则。Feign 始终保留逐文件 `compatibility-only` 边界，不能因同批 Controller 而套用新端点风格。

同一连续上下文只对同一 `loaded_rules_hash` 注入一次加载要求。该字段表示要求已经送入上下文，不证明 Agent 已读取正文。resume、compact 或新 Agent 重新确认所需资产。

## 5. 事件契约

| 工具事件 | 内部事件 | 行为 |
| --- | --- | --- |
| SessionStart | `session_initialized` / `context_restored` | 初始化或恢复上下文 |
| SessionEnd | `session_ended` | 保存恢复状态 |
| UserPromptSubmit | `task_intent_received` | 识别代码意图和用户决策 |
| SubagentStart | `agent_started` | 独立确认子 Agent 规则 |
| PreToolUse | `before_code_write` | 写入前加载和基线 |
| PostToolUse | `after_code_write` | 文件跟踪和检查失效 |
| Stop | `before_task_finish` | 整批 diff 验收 |
| Codex Interrupt | `turn_interrupted` | 保存中断后的可恢复状态 |
| Qoder PostToolUseFailure | `after_code_write` | 可能部分写入后重新检查 |
| Qoder PreCompact | `context_restored` | 失效加载状态，后续事件重新加载 |

Codex 使用 `SessionStart(compact)` 恢复，不同时接入 PostCompact。Qoder 当前目标为 IDE，不包含 CLI；缺少 Interrupt 时依靠失败事件和 Stop 的真实工作区差异兜底。工具协议以对应版本实际行为为准。

## 6. 状态与指纹

默认用户数据目录为 `~/.coding-rules-loop/`，可由 `CODING_RULES_LOOP_HOME` 覆盖。规则在 `rules/`，状态在 `state/`，观测在 `observations/`。这些运行数据不放进代码仓库。

状态按仓库和会话隔离，在同一锁内合并主 Agent 与子 Agent。规则组合包含 Common、专业 Skill、详细规范的版本摘要以及沿路径生效的项目规则。项目约束优先于个人偏好。

```json
{
  "check": {
    "diff_hash": "current-task-hash",
    "file_hashes": {"src/OrderService.java": "file-hash"},
    "tool_status": "pending",
    "semantic_status": "pending",
    "continuation_count": 1
  }
}
```

`pending` 表示尚未完成检查。只有实际验证和语义复核完成后才能回写 `passed`；指纹收集成功不等于工具检查通过。

基线记录任务开始时的 HEAD、已有脏文件和未跟踪文件摘要。整批相关变化包括工具触达文件、任务后新增文件和相对基线变化文件。既有脏文件若未触达且未再变化，不自动纳入修复。

任务总指纹由按路径排序的文件子指纹和规则组合指纹生成。覆盖删除、重命名、文件模式和未跟踪内容，任务中途提交后仍按原基线验收。文件子指纹定位变化，总指纹决定是否重新检查。

## 7. Codex 结果回写与运行模式

Stop 尚未完成检查时返回 `decision: block` 和检查提示，驱动当前 Agent 继续。提示包含当前指纹、相关文件、适用规则、验证要求和结果格式。Agent 在正常回复末尾附加 HTML 注释形式的 JSON，Adapter 从 `last_assistant_message` 提取：

```text
<!-- coding-rules-loop-result
{"diff_hash":"current-task-hash","tool_status":"passed","semantic_status":"passed","findings":[]}
-->
```

结果必须显式提供当前指纹和工具、语义状态。旧指纹、格式无效、不完整或非法结果重新检查。该协议传递 Agent 的结论，不是独立模型或项目测试的替代品；不同客户端是否隐藏注释需要实机确认。

| 结果 | 含义 |
| --- | --- |
| `passed` | 当前指纹的两类检查均通过，没有剩余问题 |
| `repairable` | 明确问题，提供规则、位置、类型和修复建议 |
| `needs_user_decision` | 业务、兼容或数据口径需用户决定 |
| `check_failed` | 检查器、验证环境或结果传递未完成 |

observe 驱动 Codex 检查和回写，记录 `would_repair`，不启动额外自动修复轮次。repair 先回写 `repairable`，Core 判断循环保护后再驱动 Agent 修改，避免绕过修复计数。

当前默认配置仍为 observe。Qoder IDE 的 Stop 暂未实现上述回写与续接，不能把 Codex 行为推定为 Qoder 已支持。

## 8. 循环保护、故障和数据边界

- 同一 diff 最多请求三次检查；没有有效结果时记录 `check_failed` 并停止自动续接。
- 自动修复最多三轮；相同问题连续两次、修复后无变化或 A → B → A 指纹循环时进入用户确认。
- 检查次数与修复轮次分开计算，检查通过后清零。
- 工具内部故障告警并放行，同时标记检查未完成，不能报告为通过。
- 当前状态只保存摘要，不持久化完整源码、diff、问题正文或修复建议；文件权限和原子替换由 StateStore 控制。
- 只处理能够归属于当前任务的变更，既有修改和边界不明确的内容先报告。

## 9. Learning

用户明确纠正、Review 确认的新问题或跨任务重复偏差可以形成 Learning 候选。先查重与冲突，再保存脱敏证据、出现次数、适用范围和建议去向。候选不自动生效，必须经过真实验证和人工确认后才能合并进正式规则。

## 10. 接入、验证与回滚

从本仓库安装时，规则目录和三个 Skill 可以分别软链接到用户目录。先核对并备份已有同名目录和 Hook 配置，再合并模板中的 Hook，不直接覆盖其他配置。完整仓库必须保留，因为 Skill 包装入口通过相对位置定位 `hooks/` 和共享 Core。

Codex 默认 Skill 根为 `~/.agents/skills/`，Qoder IDE 为 `~/.qoder/skills/`。可用 `CODING_RULES_LOOP_SKILLS_ROOT` 指定其他位置。本次文件复制不安装全局配置，也不修改已有全局软链接。

最小本地验证命令在[开发计划](编码规则Loop开发计划.md)中维护。实机必须覆盖只读负向、Java、Controller、Feign、检查回写、新指纹复查和循环保护。fixture 通过只能证明本地协议与状态行为。

回滚时禁用新增 Hook 或恢复原配置，恢复原软链接；保留用户规则、Learning、状态和观测数据。源仓库与通用副本独立，日后同步采用文件比较和白名单复制，不合并私有 Git 历史。
