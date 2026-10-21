---
name: coding-rules-loop
description: 在 Git 仓库中实现、修复、重构或补充测试时使用；负责加载全局 Common 与适用的专业 Skill，按当前任务相关 diff 完成检查、修复、复查，并把重复纠正整理为 Learning 候选。只读分析和纯文档任务不使用。
---

# Coding Rules Loop

本 Skill 编排两个闭环：当前任务的“加载、检查、修复、复查”，以及跨任务的“记录、验证、提炼、人工晋升”。规则内容仍由全局 Common 和专业 Skill 维护，本文件不复制它们的正文。

## 适用边界

仅在 Git 仓库内的代码相关任务启用。非 Git 目录、只读查询、文章阅读和普通文档修改直接跳过，不加载编码规则、不执行 diff 语义检查或修复，也不把跳过记为检查通过或检查失败。代码相关配置、SQL、测试以及规则入口仍属于代码相关变更。

只读或文档回合不因同一会话之前修改过代码而进入检查；后续真正的代码写入重新激活流程。文档意图不预热规则，混合任务由实际代码写入激活；收尾检查只包含代码相关文件。

## 规则加载

1. 先遵守目标项目的 `AGENTS.md` 和真实构建、接口契约；项目约束高于全局个人偏好。
2. 设置 `CODING_RULES_LOOP_HOME` 时，从其 `rules/coding-rules.md` 读取 Common；否则读取 `~/.coding-rules-loop/rules/coding-rules.md`。不要把 `coding-rules-learning.md` 当作编码规则加载。
3. 修改 Java 时读取 `coding-standards-java`；触及 Controller、请求响应、OpenAPI/Swagger 或 Feign 时再读取 `api-design`。
4. 只有对应主 Skill 的加载条件满足时才读取完整规范。Feign 只执行远程契约兼容边界。
5. Hook 列出待加载资产时，完整读取当前上下文尚未加载的文件；同一 `loaded_rules_hash` 不重复读取。resume、compact 或新子 Agent 需要重新确认上下文。

Hook 未启用或执行失败时，也按上述规则完成本次任务；不要因为 Loop 不可用而阻塞正常开发。

## 当前任务检查—修复 Loop

- 只处理当前任务新增、修改或明确触达的代码，不整改未触达的历史实现。
- 将 Controller、Application、Domain、Gateway、Feign 和测试等相关文件作为一批检查，不能用逐文件结果替代上下游语义检查。
- 开始修改前确认目标、项目版本、现有分层、外部契约和已有脏文件；不能证明归属于本任务的既有修改不自动改写。
- 完成实现后执行项目已有的最小必要 formatter、静态检查、编译或测试，并按 Common 与专业 Skill 复核当前相关 diff。
- 正常实现过程中已经确认的明确问题直接完成最小修复；Stop 检查新发现的问题先回传 `repairable`，由公共 Core 通过循环保护后再按 Adapter 续接完成修复。业务口径、接口兼容、数据影响或规则冲突存在歧义时咨询用户。
- 修复使 diff 变化后重新检查。循环轮次、重复问题和重复指纹由公共 Core 控制，不自行绕过保护状态。
- Stop 要求收尾检查时，按 Adapter 给出的当前 diff 指纹、文件和规则清单完成复核与最小必要验证，并回传真实结构化结果。没有执行的验证不得标记为通过，旧指纹的结果不得用于新 diff。

`observe` 会驱动检查和结果回写，但不根据新发现的问题启动额外自动修复轮次；`repair` 才会在循环保护范围内继续驱动修复与复查。两种模式都不能忽略规则，当前 Agent 仍应在正常实现过程中修复已经确认的问题，并在结束前完成相称验证。

## Learning Loop

用户明确纠正、Review 确认问题或多个任务重复出现同类偏差时，读取 [Learning 候选处理](references/learning-loop.md)。只整理候选和证据，不自动修改 Common 或专业 Skill。
