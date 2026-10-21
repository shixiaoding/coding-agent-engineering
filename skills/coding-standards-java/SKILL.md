---
name: coding-standards-java
description: 编写、修改或审查 Java/Spring 源码与测试时使用；普通修改执行核心规则，涉及新模块、版本兼容、分层、事务并发、性能或测试策略时再读取完整规范；API 契约由 api-design 补充。
---

# Java 编码与兼容规范

先读取目标项目的 `pom.xml`、Gradle 配置、formatter/Checkstyle 和局部规则，确认 Java、Spring 与 Jakarta/Javax 版本。

项目事实和已发布契约优先。不得为了应用新规范改造未触达的存量代码，也不得引入目标版本不支持的 record、sealed class、新 switch 或框架写法。

Controller、请求/响应模型及 OpenAPI 还需加载 `api-design`；Feign 的接口形态服从远程契约，只执行 `api-design` 的兼容边界。

## 加载方式

普通 Java 业务修改、缺陷修复和局部重构只执行本文件核心规则。

出现以下任一情况时，必须同时读取 [完整 Java 规范](references/java-spec.md)：

- 新建项目、模块或一组承担新职责的类型。
- 使用或调整 Java/Spring 版本相关语法和注解。
- 调整 DDD 分层、依赖注入、DTO 映射或公共类型结构。
- 修改事务、并发、线程池、缓存、锁或批处理策略。
- 设计测试策略、集成测试或测试基础设施。
- 调整查询性能、监控注解或统一日志方式。
- 核心规则在目标项目中存在多种合理实现或与项目现状冲突。

reference 不随本 Skill 自动全量加载。

## [MUST] java.no-hardcoded-secrets

不得在源码、测试或提交配置中写入真实 API Key、Token、密码和私钥。测试占位值必须明显为非真实凭据；真实密钥使用项目认可的配置或密钥设施。

## [MUST] java.parameterized-sql

用户输入和外部输入不得通过字符串拼接进入 SQL。使用参数绑定；动态表名、列名和排序字段使用明确白名单。

## [MUST] java.validate-boundary-input

外部请求、消息、文件和第三方响应进入系统时完成必要的必填、格式和值域校验。已有边界校验可靠覆盖后，后续层只处理业务约束，不重复兜底。

## [MUST] java.no-sensitive-log-data

日志不得输出密码、Token、密钥、Cookie、完整身份凭证和未脱敏的敏感个人信息。保留定位所需的业务标识和异常上下文时，遵循项目脱敏约定。

## [MUST] java.safe-client-errors

对外响应不得暴露堆栈、SQL、内部路径、凭据或未经筛选的异常消息。异常处理保留真实原因和有效上下文，不静默吞错，也不做无信息增量的包装。

## [SHOULD] java.standard-naming

类型使用 PascalCase，方法、字段、参数和局部变量使用 camelCase，常量使用 UPPER_SNAKE_CASE，包名使用全小写。

业务命名直接表达用途，避免 `flag`、`data`、`temp` 等无语义名称；目标项目已有命名约定优先。

## [SHOULD] java.optional-no-blind-get

`Optional#get()` 前必须有可证明的存在性检查，优先用 `map`、`flatMap`、`orElseThrow` 等表达缺失分支。

不把 `Optional` 作为字段或参数，除非项目既有契约明确要求。

## [SHOULD] java.stateful-flow-prefers-loop

同时修改状态、收集副作用参数或合并多组集合时，优先使用普通循环和清晰阶段变量。Stream 适合单向筛选、映射和汇总，不在流水线中隐藏副作用。

## [SHOULD] java.collection-match-by-semantics

集合天然可能一对多时，不默认取第一条作为业务结果。根据业务语义直接匹配、分组或显式校验唯一性；只有上游契约已经保证唯一和顺序时，才按固定位置取值。

## [SHOULD] java.semantic-dto-top-level-file

有独立业务语义或跨方法流转的 DTO 放在独立文件中。只在当前实现内部短暂使用且没有复用价值的临时结构可以保留为内部类型。

## [SHOULD] java.tests-deterministic-behavior

测试名称和断言描述业务场景与预期结果，避免隐藏的 sleep、时间竞争和执行顺序依赖。优先覆盖本次行为与回归风险，不为固定覆盖率编写只复述实现的测试。

## 修复边界

命名、格式、无用引用、盲目调用 `Optional#get()` 和明显的 Stream 副作用等口径明确的问题，可以直接完成最小修复，并运行项目已有工具验证。

事务边界、异常语义、并发策略、领域分层、注入方式、性能优化或版本兼容存在多种合理选择时，先结合构建配置和调用链判断；仍有歧义再询问用户。

## Java 变更完成前检查

- [ ] 已读取目标项目版本、构建配置、formatter/Checkstyle 和局部规则；需要时已读取完整规范。
- [ ] 新增语法、API 和框架注解受目标 Java/Spring 版本支持。
- [ ] 密钥、SQL、输入边界、日志和对外错误满足对应 `MUST` 规则。
- [ ] 类型、方法、字段、参数、常量和包命名满足 `java.standard-naming`。
- [ ] Optional 和 Stream 用法清晰，没有隐藏缺失分支或状态副作用；一对多集合没有无依据地取固定位置。
- [ ] 有独立语义的 DTO 使用独立文件，临时内部类型没有被过度抽象。
- [ ] 新增或修改的测试描述业务行为、覆盖本次风险且保持确定性。
- [ ] 没有引入无业务语义的中间层、方法拆分或过程态对象。
- [ ] 关键注释解释业务原因和约束，没有翻译代码动作。
- [ ] 已清理本次变更产生的无用 import、参数、方法和分支，并完成受影响验证。
