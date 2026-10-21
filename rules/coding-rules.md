# 通用编码规则

这是 Coding Rules Loop 的通用正式入口，保存可按项目调整的编码偏好与检查标准。编码任务先读取本文件，再按文件类型加载专业 Skill。

`coding-rules-learning.md` 只保存尚未确认的候选，不参与普通编码，也不会自动修改本文件或专业 Skill。候选只有经过真实任务验证和人工确认后才能晋升。

目标项目的真实版本、构建配置、既有接口契约和项目 `AGENTS.md` 高于本文件中的个人偏好；安全、语言正确性和已确认业务口径不允许被个人偏好覆盖。

## 执行口径

- 只检查和修复当前任务新增、修改或明确触达的代码，不主动整改未触达的历史代码。
- `MUST` 的明确问题必须修复；存在业务、兼容或数据影响歧义时咨询用户。
- `SHOULD` 存在合理例外时可以说明原因后保留。
- 案例用于解释判断标准，不要求照搬其中的类名、分层、技术组件和写法。
- 修复导致 diff 变化后必须重新检查。

## 启用的专业 Skill

- `coding-standards-java`：修改 Java 源码或测试时加载主 Skill；新模块、版本、分层、事务并发、性能或测试策略变化时，由主 Skill 继续加载完整规范。
- `api-design`：触及 Controller、请求响应、OpenAPI/Swagger 或 Feign 时加载主 Skill；新增接口和契约变更按需加载完整规范，Feign 只执行兼容边界。
- 专业 Skill 的 `references/` 不默认全量加载，由对应主 Skill 根据任务内容决定。

## 编码完成检查

- [ ] 已确认目标项目的语言与框架版本、构建配置、格式化工具、既有分层和外部契约。
- [ ] 没有重复查询可信上游已经提供的数据，协议类型和空值语义已在正确边界收口。
- [ ] 新增对象、分层和方法具有明确语义，没有机械转发、拆参或字段搬运。
- [ ] 简单结果直接表达，复杂结果一次性组装；复杂流程按业务阶段组织，不同阶段之间保留空行。
- [ ] 没有为了形式统一机械换行；Java 以 140 字符为软参考，实际服从项目工具和可读性。
- [ ] 业务方法和关键私有业务方法有必要的方法级说明；其他注释只解释业务原因和约束，TODO 写明原因及删除条件。
- [ ] 没有新增可变静态任务状态或单例任务过程态。
- [ ] 已清理本次变更产生的无用引用、依赖、参数、方法和分支，并验证受影响行为。

## [SHOULD] coding.reuse-trusted-upstream

上游已经返回完整且口径可信的数据时优先直接复用。只有存在明确字段或口径缺口时才补查，避免重复查询、补数和机械映射。

适用：当前流程已经从同一数据源获得与后续使用相同口径、权限范围和时效性的数据，却再次查询相同结果。

允许例外：向下游传递 ID、查询不同聚合或明细、上游缺少必要字段、数据时效或权限口径不同，或者目标查询承担独立业务校验。传 ID 本身不属于重复获取。

反例：

```java
OrderResult order = orderGateway.getOrder(orderNo);
String customerName = orderGateway.getOrder(orderNo).getCustomerName();
```

推荐：

```java
OrderResult order = orderGateway.getOrder(orderNo);
String customerName = order.getCustomerName();
```

这里的问题是同一任务内重复调用同一数据源获取同一字段，不是“后续方法只能接完整对象”。如果无法确认两个查询是否同源同口径，不删除查询，先核对数据来源和调用方需求。

## [SHOULD] coding.normalize-at-boundary

外部协议的类型转换、编码标准化和空值归一化优先在 gateway、repository 或 convert 边界完成，内部流程消费语义稳定的模型。

内部模型保持内部口径；只有真正调用外部服务或组装外部参数时才转换为外部类型。动态默认值和业务决策由掌握上下文的调用层处理，不伪装成通用转换。

反例：

```java
ExternalStoreRes res = storeClient.query(storeNo);
Integer regionCode = Integer.valueOf(res.getRegionCode());
return storeApplication.process(regionCode);
```

推荐：

```java
StoreResult store = storeGateway.query(storeNo); // Gateway 已完成外部类型归一化
return storeApplication.process(store.getRegionCode());
```

null、空集合和默认值在同一调用链中保持一致语义；口径不明确时先确认，不自行补 fallback。

## [SHOULD] coding.no-semanticless-layer

DTO、Command、Args、Param、Resolver、Validator、Gateway、Convert 或 Service 必须承载协议隔离、业务语义、复用或复杂度收敛价值。只做同构字段搬运、基础参数拆装或向下一层转发时，不新增中间对象和薄方法。

只依赖对象自身状态的业务判断和值域映射，优先由领域对象、状态对象或枚举承载；不要为一句判断新增薄 Service、Resolver 或 Validator。

适用：本次新增的分层、对象和辅助方法，以及本次主动调整的触达链路。不要求为本规则重构未触达的历史结构。

允许例外：

- 隔离 HTTP、Feign、MQ、数据库等外部协议。
- 多个入口需要收敛为同一个内部操作。
- 多个参数共同表达一个完整业务动作。
- 转换包含类型归一化、字段口径转换或稳定映射。
- 中间对象明显降低复杂度或形成真实复用。

多个参数被重复成组传递，并共同表达稳定查询条件、存储键、外部身份或完整业务动作时，优先收口为项目认可的语义对象。是否创建对象以业务语义、复用和复杂度收益为准，不以固定参数数量决定；对象名称、后缀和包位置服从项目现有的 DTO、Args、Param、Command 或其他约定。

参数只在单次简单调用中使用，或者新对象只会机械复制字段时，不为缩短参数列表额外包装。

反例：

```java
public Boolean save(OrderSaveReq req) {
    return orderApplication.save(req.getOrderNo(), req.getRemark());
}
```

请求与应用服务表达相同语义且边界校验已经完成时，优先直接传递：

```java
public Boolean save(OrderSaveReq req) {
    return orderApplication.save(req);
}
```

存在协议隔离时，可以转换为项目认可的内部语义对象，例如 Command、DTO、Args、Param 或项目已有类型。具体类型、后缀、包位置和转换方式服从项目规范，不为应用本规则引入新的命名体系。

简单配置、字典等 CRUD 没有领域规则时，允许按项目习惯直接完成 Req 到 DO 的转换，不强制增加领域层。

## [SHOULD] coding.assemble-final-once

简单场景直接构造或返回最终结果；复杂场景先准备业务数据，再一次性输出最终 DTO 或 VO，不把最终对象当作跨分支持续修改的过程态容器。

反例：

```java
OrderVO vo = new OrderVO();
vo.setOrderNo(order.getOrderNo());
if (member != null) {
    vo.setMemberName(member.getName());
}
vo.setStatus(resolveStatus(order));
return vo;
```

推荐先收敛业务数据，再统一组装：

```java
String memberName = member == null ? null : member.getName();
OrderStatus status = resolveStatus(order);
return OrderVO.of(order.getOrderNo(), memberName, status);
```

一次性组装不等于所有对象必须不可变；目标项目使用普通 Java Bean 时，可以沿用项目构造或转换方式。

## [SHOULD] coding.split-by-semantics

同时包含校验、外部调用、数据落库或状态流转的复杂流程，按业务阶段组织主流程；简单流程不机械套用阶段模板。

只有拆分能明确业务阶段、复用点或边界时才提取方法。单次使用且只包装少量判断或赋值的逻辑优先就地表达。

推荐的复杂主流程：

```java
public Result execute(Request req) {
    // 1、校验当前请求
    validateRequest(req);

    // 2、获取外部业务结果
    ExternalResult external = queryExternalResult(req);

    // 3、保存本次业务结果
    DomainResult result = saveBusinessResult(req, external);

    // 4、组装最终返回结果
    return Result.from(result);
}
```

其中每个方法应承载明确阶段；如果 `buildXxx`、`fillXxx` 或 `resolveXxx` 只包装一两行赋值且没有独立语义，应直接内联。

复杂主流程使用 `// 1、业务阶段`、`// 2、业务阶段` 的中文编号注释标识处理阶段，例如“获取门店信息”“组装门店信息”。同一阶段的连续代码紧凑书写，不同业务阶段之间保留一个空行，让主流程形成清晰的视觉分段。

阶段数量不设固定值；简单流程不为满足形式机械添加编号注释或空行。

## [SHOULD] coding.avoid-mechanical-line-wrap

代码换行服从目标项目的 formatter、静态检查和现有格式约定。没有项目硬约束时，以可读性和语义边界决定是否换行，不为形式统一机械拆分结构清楚的声明、注解或调用。

Java 单行以 140 字符作为软参考线，不作为强制阈值。未超过参考线的代码保持单行；单个注解、单参数方法声明或结构清楚的方法调用即使略微超过参考线，也可以保持单行。

多参数、长调用链或多层表达式已经影响阅读时，按参数或业务语义边界换行；换行后的同层级内容保持对齐。不为应用本规则重新格式化未触达的历史代码。

反例：

```java
public StoreProgressQueryResult
        queryStoreProgressByTaskAndOrganizationScopeWithCurrentUserPermissions(
                StoreProgressQueryRequest request) {
```

单参数声明略微超过软参考线但仍清楚时，推荐保持单行：

```java
public StoreProgressQueryResult queryStoreProgressByTaskAndOrganizationScopeWithCurrentUserPermissions(StoreProgressQueryRequest request) {
```

## [SHOULD] coding.comments-explain-why

新增注释默认使用中文，只解释业务口径、边界条件、兼容原因、顺序要求、历史原因或外部限制。

新增或修改的方法如果承担业务编排、状态变化、跨系统适配、历史兼容、顺序约束、降级或非显然返回语义，应使用目标语言认可的方式提供方法级说明。说明业务目的和关键约束；入参、返回值或异常语义不直观时补充对应说明。

承担关键业务阶段、兼容或降级语义的私有方法同样需要说明。简单判空、取值、字段转换、getter/setter 和一到三行辅助方法不机械补方法注释。

不复述变量、方法、异常码、赋值和显而易见的判断。复杂主流程的编号阶段注释可以说明当前处理步骤，这是流程可读性的明确例外。

反例：

```java
// 设置用户 ID
result.setUserId(userId);
```

推荐：

```java
// 老版客户端仍按 channelCode 判断入口权限，完成全量升级前必须继续返回。
result.setChannelCode(channelCode);
```

TODO 必须写清原因、触发条件或删除时机：

```java
// TODO 等旧版客户端全部下线后，删除 channelCode 兼容字段。
```

如果注释删除后代码本身已经表达清楚，就不添加；如果维护者可能追问“为什么不能这样改”，应保留原因和边界。

## [MUST] coding.no-mutable-singleton-task-state

共享单例和静态字段不得保存计数、进度、失败明细或请求过程态。单次任务状态使用局部变量或 per-execution context。

反例：

```java
@Service
public class ImportService {
    private final AtomicInteger successCount = new AtomicInteger();
}
```

推荐：

```java
public ImportResult execute(ImportRequest request) {
    int successCount = 0;
    // 本次任务的计数只在当前执行上下文中维护。
}
```

确需共享的缓存、锁和并发组件必须有明确生命周期与线程安全设计；不能用“当前只会单线程执行”的注释替代并发安全。

## [SHOULD] coding.clean-touched-dead-code

本轮已确认删除的实现、参数或重载，其触达调用链上的无用方法、依赖和 import 一并清理，不保留“以后可能有用”的半废弃状态。

清理范围只覆盖本次已经确认和触达的链路，不扩散到未确认业务。无法证明没有调用方时，先查询引用和运行契约，不直接删除。
