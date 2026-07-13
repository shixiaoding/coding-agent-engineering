# Coding Agent Engineering

面向 Coding Agent 的可复用工程化资产库。

本仓库为 Codex、Claude Code、Cursor、Qoder 等工具提供可移植的工程规则、任务工作流与专项 Skill，帮助 Agent 在真实项目中先理解约束，再完成开发、验证与交付。

它不是某个 Agent 的配置合集，也不试图接管整个研发过程；每个组件都应保持小、清晰、可组合，并能按项目实际情况调整。

## 解决什么问题

- Agent 缺少项目约束，容易在不了解背景时直接修改代码。
- 同类任务反复依赖临时提示词，输出质量和流程不稳定。
- 团队规则、任务步骤和专项能力混在同一个提示词里，难以维护和复用。

## 当前组件

| 组件 | 用途 | 当前状态 |
| --- | --- | --- |
| [AGENTS.md](AGENTS.md) | 工具无关的通用协作规则 | 可直接放入项目根目录使用 |
| [CLAUDE.md](CLAUDE.md) | Claude Code 兼容入口 | 与通用规则保持一致 |
| [skills/rg-code-review](skills/rg-code-review) | 团队级增量代码审查 Skill | 已可用 |
| [docs/architecture.md](docs/architecture.md) | 组件边界与演进原则 | 已可用 |

## 使用方式

按需选用组件，不需要一次性安装整个仓库。

### 复用项目协作规则

将 [AGENTS.md](AGENTS.md) 复制到目标项目根目录，再基于项目语言、构建方式和业务边界补充项目专属规则。

```bash
cp /path/to/coding-agent-engineering/AGENTS.md /path/to/your-project/AGENTS.md
```

`AGENTS.md` 负责通用协作约束；不要把某个业务项目的领域规则反向写回本仓库。

### 安装单个 Skill

以 `rg-code-review` 为例，将 Skill 目录复制到所使用工具的 skills 目录。不同工具的发现路径可能不同，请以对应工具的官方文档为准。

```bash
cp -R skills/rg-code-review ~/.codex/skills/
```

### 在项目中执行

推荐顺序：先读取项目规则与上下文，再选择合适的 Skill 或工作流，最后运行最小必要的验证。不要让 Agent 因为安装了本仓库而跳过项目自身约束。

## 组件边界

```text
规则（rules）      约束：哪些事必须做、哪些事不能做
工作流（workflows）步骤：一个任务应如何推进和验收
Skill               能力：在特定触发条件下可复用的执行方法
模板（templates）  载体：需求、设计、评审等文档的起点
工具适配（adapters）差异：同一能力如何接入不同 Coding Agent
```

当前仓库已沉淀通用规则和代码审查 Skill。其余目录只在出现第一个可复用、经过实际验证的组件时创建，避免空目录和形式化资产。

详细边界见 [架构说明](docs/architecture.md)。

## 与 `skills` 仓库的关系

[`skills`](https://github.com/shixiaoding/skills) 面向个人学习、研究、写作、复盘等通用工作；本仓库面向软件研发中的 Coding Agent。

```text
skills                     我如何借助 Agent 完成个人高频工作
coding-agent-engineering   Agent 如何在工程项目中可靠地工作
```

## Roadmap

- [x] 通用 Agent 协作规则
- [x] 团队级增量代码审查 Skill
- [ ] 从真实项目中提炼需求澄清、缺陷修复与交付验证工作流
- [ ] 补充 Codex、Claude Code 等工具的最小适配说明
- [ ] 为稳定组件补充示例与验证方式

## 参考

- [mattpocock/skills](https://github.com/mattpocock/skills)：小型、可组合 Skill 的组织方式
- [affaan-m/ECC](https://github.com/affaan-m/ecc)：多 Agent 工程化能力的组织视角

本仓库只借鉴其“模块化、可适配、以真实工程反馈为准”的原则，不复制其完整框架或安装体系。

## License

MIT
