# Python Capability Runtime 设计文档

本文档定义 `python/` 目录下 Python agent 的“能力扩展运行时”设计。

目标不是复刻原始 `pi` 的全部产品能力，而是先为 agent 核心链路建立可扩展、可重载、可迭代的基础设施，让后续的工具、提示、命令、上下文、会话压缩策略能够在尽量少改核心代码的前提下接入。

## 1. 背景

当前 Python 实现已经具备一条可用的最小核心链路：

- OpenAI 兼容 provider
- 流式文本输出
- 工具调用循环
- 工作区内置工具
- 本地会话持久化
- `/reload` 手动重载

但这些能力目前主要以硬编码方式存在于以下模块中：

- `src/pyi/agent.py`
- `src/pyi/tools.py`
- `src/pyi/cli.py`
- `src/pyi/config.py`

这导致两个直接问题：

1. 新能力必须修改核心源码后才能接入。
2. agent 无法较自然地“生成能力并安装能力”，只能“生成补丁并修改自己”。

如果目标是提高 agent 核心链路的扩展性，为未来的自我升级和自我迭代打基础，那么第一优先级应当是把核心链路从“硬编码程序”演进为“可装配运行时”。

## 2. 设计目标

### 2.1 主要目标

1. 让工具、命令、提示片段、上下文资源、会话压缩策略可注册、可发现、可重载。
2. 保持当前 Python 项目的范围聚焦在 agent 核心链路，不引入 TUI、RPC UI、主题系统等高成本表层能力。
3. 尽量复用现有 `Agent`、`SessionStore`、`ConfigManager`、`OpenAICompatibleProvider` 的结构，避免一次性大改。
4. 允许后续引入类似原项目 `Extensions`、`Skills`、`Prompt Templates` 的最小对应物。
5. 允许 agent 在工作区内创建或修改能力包，并通过 `/reload` 载入新能力。

### 2.2 非目标

当前阶段不包括：

- TUI 对齐
- RPC 模式
- 完整 provider 生态
- 完整 npm/git 包生态
- 任意不受约束的第三方代码执行模型
- 自动文件监听

## 3. 设计原则

### 3.1 先资源，后代码

优先支持声明式能力资源：

- prompt 文件
- skill 说明文件
- command/tool manifest
- compaction policy 配置

之后再支持受控的 Python 代码扩展点。

### 3.2 可重载优先

所有新能力都应优先考虑是否能被 `/reload` 重新加载，而不是要求重启进程。

### 3.3 核心链路最少感知

`Agent` 不应知道某个具体扩展来自哪个目录、哪种包格式、哪个提供方。它只依赖统一注册表与 hook 接口。

### 3.4 工作区边界不放松

无论能力如何扩展，文件和 shell 工具仍应默认保持工作区边界限制。扩展系统不能绕过现有路径安全策略。

### 3.5 增量迁移

先把现有内置能力迁移到统一 registry，再开放外部能力加载。不要直接设计一个“大而全”的插件框架。

## 4. 总体架构

新增一个统一的运行时层：`CapabilityRuntime`。

它负责：

- 加载能力资源
- 注册工具、命令、提示提供者、上下文提供者、压缩策略
- 暴露给 `Agent` 和 `CLI` 的统一查询接口
- 执行 reload
- 维护加载诊断信息

建议新增模块结构：

- `src/pyi/runtime/__init__.py`
- `src/pyi/runtime/runtime.py`
- `src/pyi/runtime/registries.py`
- `src/pyi/runtime/loader.py`
- `src/pyi/runtime/hooks.py`
- `src/pyi/runtime/resources.py`
- `src/pyi/runtime/diagnostics.py`
- `src/pyi/runtime/builtin_capabilities.py`

可选后续模块：

- `src/pyi/runtime/skills.py`
- `src/pyi/runtime/prompts.py`
- `src/pyi/runtime/compaction.py`
- `src/pyi/runtime/extensions.py`

## 5. 核心对象模型

### 5.1 CapabilityRuntime

`CapabilityRuntime` 是新的中心对象。

建议职责：

- 持有所有 registry
- 持有 loader 和 diagnostics
- 提供 `reload()`
- 提供只读查询接口
- 构造 agent 运行时视图

建议接口：

```python
@dataclass(slots=True)
class RuntimeSnapshot:
    tools: dict[str, ToolSpec]
    commands: dict[str, CommandSpec]
    prompt_fragments: list[PromptFragment]
    context_sources: list[ContextSource]
    compaction_strategy: CompactionStrategy | None
    diagnostics: RuntimeDiagnostics


class CapabilityRuntime:
    def snapshot(self) -> RuntimeSnapshot: ...
    def reload(self) -> RuntimeReloadResult: ...
    def get_tool(self, name: str) -> ToolSpec | None: ...
    def get_command(self, name: str) -> CommandSpec | None: ...
```

### 5.2 Registries

建议至少定义 5 个 registry：

1. `ToolRegistry`
2. `CommandRegistry`
3. `PromptRegistry`
4. `ContextSourceRegistry`
5. `CompactionRegistry`

共同特点：

- 支持注册内置能力
- 支持注册外部能力
- 支持按名称查询
- 支持冲突检测
- 支持来源跟踪

### 5.3 Loader

`CapabilityLoader` 负责从多个来源加载能力定义。

建议支持的来源顺序：

1. 内置能力
2. 全局用户目录能力
3. 项目目录能力
4. 显式配置启用的附加目录

后加载的来源可以覆盖前者，但必须输出冲突诊断。

### 5.4 Hook Pipeline

为避免把扩展逻辑散落在 `Agent` 中，引入统一 hook。

建议第一阶段支持：

- `before_prompt_build`
- `after_prompt_build`
- `before_provider_request`
- `after_provider_event`
- `before_tool_execute`
- `after_tool_execute`
- `before_session_save`
- `compact_session`

这些 hook 不要求一开始都开放给外部代码实现，但内部结构应预留。

## 6. 能力类型定义

### 6.1 Tool

工具仍然沿用当前 `ToolSpec` 的基本结构，但加入来源与可见性元数据。

建议扩展：

```python
@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    handler: ToolHandler
    source: str
    kind: Literal["builtin", "extension"] = "builtin"
    enabled_by_default: bool = True
```

设计要点：

- 保持现有 `execute_tool()` 模型不变
- 内置工具先通过 `builtin_capabilities.py` 注册
- 后续允许外部模块注册新的 `ToolSpec`
- 工具启用状态由 runtime config 与 registry 共同决定

### 6.2 Command

新增 `CommandSpec`，把 CLI 里的 slash command 硬编码分支收敛到 registry。

建议结构：

```python
@dataclass(slots=True)
class CommandContext:
    agent: Agent
    runtime: CapabilityRuntime
    args: argparse.Namespace


@dataclass(slots=True)
class CommandSpec:
    name: str
    summary: str
    handler: CommandHandler
    source: str
```
```

第一阶段应先把这些现有命令注册化：

- `/help`
- `/model`
- `/base-url`
- `/tools`
- `/sessions`
- `/switch`
- `/fork`
- `/rename`
- `/reload`
- `/save`
- `/exit`

`/reload code` 可以继续保留为内部开发命令，但不应成为扩展机制本身的核心路径。

### 6.3 Prompt Fragment

当前只有单个 `system_prompt` 字符串，不足以支撑能力叠加。

建议引入 `PromptFragment`：

```python
@dataclass(slots=True)
class PromptFragment:
    name: str
    priority: int
    text: str
    source: str
    applies_when: Callable[[PromptContext], bool] | None = None
```

最终系统提示由多个 fragment 合成：

- 内置基础系统提示
- 项目规则提示
- 工具使用约束提示
- skill 注入提示
- 用户显式选择的模板提示

这一步是“技能”和“提示模板”后续落地的核心基础。

### 6.4 Context Source

新增 `ContextSource`，为 provider request 构建前补充上下文。

建议用途：

- 加载工作区说明文件
- 加载 `.pyi/` 下的 agent 规则文件
- 加载 skill 文档
- 加载项目局部上下文摘要

建议结构：

```python
@dataclass(slots=True)
class ContextItem:
    kind: str
    title: str
    content: str
    source: str


class ContextSource(Protocol):
    def load(self, ctx: PromptContext) -> list[ContextItem]: ...
```

### 6.5 Compaction Strategy

为后续 `/compact` 和长会话自维护能力打基础，定义压缩策略接口。

```python
class CompactionStrategy(Protocol):
    def compact(self, session: Session, model: str) -> CompactionResult: ...
```

初始阶段可以只支持手动调用，不做自动 compaction。

## 7. 能力资源目录布局

建议支持两类目录：全局目录与项目目录。

### 7.1 全局目录

- `~/.pyi-python/capabilities/`
- `~/.pyi-python/prompts/`
- `~/.pyi-python/skills/`

### 7.2 项目目录

- `.pyi/capabilities/`
- `.pyi/prompts/`
- `.pyi/skills/`
- `.pyi/context/`

### 7.3 推荐文件格式

第一阶段优先支持：

- `.md`：prompt/skill/context 文本资源
- `.yaml`：manifest、启用状态、优先级、元信息
- `.py`：受控代码扩展入口（后续阶段）

建议 manifest 示例：

```yaml
kind: prompt
name: safer-editing
priority: 40
file: safer-editing.md
```

```yaml
kind: command
name: compact
entrypoint: mypkg.commands:compact_command
```

## 8. 配置模型扩展

在现有 `RuntimeConfig` 基础上扩展：

```python
@dataclass(slots=True)
class CapabilityPathsConfig:
    global_paths: list[str] = field(default_factory=list)
    project_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CapabilitiesConfig:
    enabled: bool = True
    paths: CapabilityPathsConfig = field(default_factory=CapabilityPathsConfig)
    allow_python_extensions: bool = False
    enabled_skills: list[str] = field(default_factory=list)
    enabled_prompts: list[str] = field(default_factory=list)
    enabled_commands: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RuntimeConfig:
    model: str | None = None
    base_url: str | None = None
    system_prompt: str | None = None
    tools: ToolRuntimeConfig = field(default_factory=ToolRuntimeConfig)
    capabilities: CapabilitiesConfig = field(default_factory=CapabilitiesConfig)
```

设计要点：

- 默认开启能力运行时，但默认仅加载内置能力与文本资源
- `allow_python_extensions` 默认关闭
- 所有路径配置都纳入 `/reload` 生效范围

## 9. Prompt 构建流程

当前 `Agent._build_provider_messages()` 直接拼接 `system_prompt`。新架构建议拆成两步：

1. `PromptAssembler` 构建最终 system prompt
2. `Agent` 仅消费组装结果

建议流程：

1. 读取基础系统提示
2. 合并启用的 prompt fragments
3. 加载上下文资源
4. 生成最终 system message
5. 附加普通消息历史

建议新增对象：

```python
@dataclass(slots=True)
class PromptBuildResult:
    system_prompt: str
    context_items: list[ContextItem]
    diagnostics: list[str]
```

这样后续 skill、模板、项目规则都不必直接修改 `Agent`。

## 10. Agent 集成方案

### 10.1 Agent 新职责

`Agent` 保持为执行协调者，但不再负责“知道所有能力从哪里来”。

建议注入：

```python
class Agent:
    def __init__(..., runtime: CapabilityRuntime, ...):
        self.runtime = runtime
```

### 10.2 Agent 使用 runtime 的方式

- provider tools 来自 `runtime.snapshot().tools`
- system prompt 来自 `PromptAssembler`
- tool hook 在执行前后调用
- compaction 由 runtime 查询是否存在策略

### 10.3 保持现有行为兼容

在没有任何外部能力时：

- 工具集合与今天一致
- slash command 行为与今天一致
- `system_prompt` 行为与今天一致
- `/reload` 继续工作

## 11. CLI 集成方案

CLI 目前最大的扩展瓶颈是 slash command 写死在 `handle_command()` 内。

改造目标：

- 内置命令先注册到 `CommandRegistry`
- `handle_command()` 只负责解析命令名并分派
- `/help` 从 registry 动态生成命令帮助

建议分阶段：

### 第一阶段

- 仅将现有命令注册化
- 不开放第三方命令代码扩展

### 第二阶段

- 支持从 manifest 或 Python 扩展注册命令
- 支持 `/skill:name` 或 `/template:name` 这种动态命名空间命令

## 12. Reload 语义

`/reload` 应成为扩展系统的标准生效路径。

建议重载内容包括：

- YAML 配置
- 工具启用状态
- prompt fragments
- context sources
- command registry
- skill manifest
- compaction strategy

建议不在 `/reload` 中默认重载任意 Python 模块对象图；Python 代码扩展如果启用，应明确通过 runtime loader 重新导入并重建 registry。

### 12.1 Reload 结果对象

建议扩展 `ReloadResult`：

```python
@dataclass(slots=True)
class ReloadResult:
    success: bool
    message: str
    applied_model: str
    applied_base_url: str
    enabled_tools: list[str]
    loaded_prompts: list[str]
    loaded_commands: list[str]
    loaded_skills: list[str]
    warnings: list[str]
```

这样 `/tools` 和未来 `/runtime` 命令都能显示更完整的状态。

## 13. 技能与提示模板的最小落地方式

### 13.1 Skills

第一阶段不实现完整 Agent Skills 标准，只实现“本地技能包”。

一个技能目录可包含：

- `skill.yaml`
- `README.md`
- 可选 prompt 片段
- 可选建议命令

`skill.yaml` 示例：

```yaml
name: code-review
summary: Help the agent perform structured code reviews.
prompt_files:
  - review-guidelines.md
context_files:
  - checklist.md
```

运行时效果：

- skill 提供额外 prompt fragments
- skill 提供额外 context items
- 后续可再扩展为 tool/command 提供者

### 13.2 Prompt Templates

第一阶段只需要解决两个问题：

1. 可命名
2. 可注入 prompt fragment

不急于实现复杂参数化模板。

## 14. 诊断与可观测性

如果没有诊断层，扩展系统会很难维护。

建议新增 `RuntimeDiagnostics`，记录：

- 已加载来源
- 成功加载的能力数量
- 名称冲突
- 无效 manifest
- Python 扩展导入错误
- 被配置禁用的能力

建议新增命令：

- `/runtime`
- `/runtime warnings`

当前已进一步落地：

- `/runtime prompt`：面向人工查看最终 assembled system prompt，以及实际采用的 fragment 顺序与来源
- `/runtime json prompt`：面向程序读取同一份 prompt inspection 数据

这些命令不是第一阶段必需，但 diagnostics 数据结构第一阶段就应该存在。

## 15. 安全边界

### 15.1 默认安全模型

默认只加载：

- 内置能力
- 用户本地文本资源
- 项目内文本资源

默认不加载任意 Python 扩展代码。

### 15.2 Python 扩展代码

若后续开启 `allow_python_extensions`：

- 仅从显式配置路径加载
- 在诊断中显示来源文件
- 不承诺沙箱隔离
- 文档中明确提示这是“完全信任模型”

### 15.3 工具边界

无论 prompt、skill、extension 如何变化：

- `read/write/edit/find/grep/ls` 的工作区路径校验不能放松
- `bash` 的超时和输出限制仍由 runtime config 控制

## 16. 迁移计划

### Phase 1: 内置能力注册化

目标：不改变外部行为，只改变内部结构。

- 引入 `CapabilityRuntime`
- 把现有内置 tools 注册到 `ToolRegistry`
- 把现有 slash commands 注册到 `CommandRegistry`
- 把基础系统提示迁移到 `PromptRegistry`
- `Agent` 改为从 runtime 取 tools 和 prompt

交付标准：

- 用户体验不变
- `/reload` 可重建 registry
- 现有会话与工具行为不变

### Phase 2: 文本资源加载

目标：支持无需写 Python 代码的扩展。

- 加载 `.pyi/prompts/*.md`
- 加载 `.pyi/skills/*/skill.yaml`
- 加载 `.pyi/context/*.md`
- 将它们纳入 prompt 构建流程

交付标准：

- agent 可通过新增文本资源增强行为
- 新资源可通过 `/reload` 生效

### Phase 3: Compaction 与动态命令

目标：增强长链路可维护性。

- 定义 `CompactionStrategy`
- 新增 `/compact`
- 支持动态命令注册
- 增加 runtime diagnostics 命令

### Phase 4: Python 代码扩展

目标：开放受控代码层扩展。

- 支持 manifest 指向 Python entrypoint
- 加载外部 tool/command/context provider
- 加入导入错误诊断

### Phase 5: Provider 抽象增强

目标：让 runtime 真正成为统一能力层。

- 将 provider 能力探测纳入 runtime snapshot
- 允许 prompt/tool 层依据 provider capability 做条件启用

## 17. 对现有文件的改造建议

### `src/pyi/agent.py`

改造重点：

- 去掉对 `build_default_tools()` 的直接依赖
- 去掉对单个 `system_prompt` 字符串的直接拼接假设
- 仅保留运行循环、provider 调用、会话写入

### `src/pyi/tools.py`

改造重点：

- 保留现有工具 handler 实现
- 把“构建默认工具集合”的职责迁出到 `builtin_capabilities.py`

### `src/pyi/cli.py`

改造重点：

- 把 slash command 分派改成 registry 驱动
- `/help` 改为动态展示已注册命令

### `src/pyi/config.py`

改造重点：

- 扩展 `RuntimeConfig`
- 支持 `capabilities.*` 配置
- 保持现有优先级规则不变

## 18. 最小可行版本范围

如果只做一个最小可行版本，建议范围严格控制为：

1. `CapabilityRuntime`
2. `ToolRegistry`
3. `CommandRegistry`
4. `PromptRegistry`
5. `/reload` 重建 runtime
6. `.pyi/prompts/*.md` 文本加载

先不要同时做：

- Python 代码扩展
- `/compact`
- 自动文件监听
- provider 多态
- 包安装系统

这样可以用最少改动获得最大的“自我迭代基础设施”收益。

## 19. 成功标准

当下面这些行为成立时，说明该设计达标：

1. 新增一个项目级 prompt 文件后，`/reload` 能让 agent 行为发生稳定变化。
2. 新增一个项目级 skill 目录后，agent 能在不改核心源码的情况下获得新约束或新工作流提示。
3. 新增一个内置命令时，不需要修改 CLI 主循环，只需要注册命令。
4. 后续新增 `/compact` 时，不需要再重构 agent 主循环。
5. agent 可以通过工具写入能力资源，再通过 `/reload` 使用这些新资源。

## 20. 总结

这个设计的核心不是“做一个插件系统”这么宽泛，而是先把 Python agent 的核心执行链路抽象成一个可装配的能力运行时。

一旦这层建立起来，后续的：

- 技能
- 提示模板
- 项目规则
- 上下文资源
- 动态命令
- 会话压缩策略
- 更丰富的 provider capability 协调

都可以作为“能力”增量接入，而不再要求持续修改核心执行代码。

这才是为 agent 自我升级和自我迭代提供基础设施的最关键一步。

## 21. 当前进度（2026-03-08）

### 已完成（Phase 1 已落地）

- 已引入最小 `CapabilityRuntime`，统一承载 tools、commands、prompts、skills 的加载结果。
- 已将内置工具改为先构建全量集合，再由 runtime 按配置筛选启用。
- 已将 CLI slash commands 改为 registry 分派，不再依赖单个长 `if/elif` 分支。
- 已将 `system_prompt` 接入 prompt 组装链路，当前支持 `builtin:base`、`config:system`、项目 prompt 文件和本地 skill 文本资源。
- 已支持 `/skill:<name>` 与 `/template:<name>` 的最小激活动作，作用域为当前进程会话。
- 已落地 `/runtime`、`/runtime warnings`、`/runtime json`。
- 已落地 `/runtime prompt` 与 `/runtime json prompt`，用于检查最终 assembled system prompt 及其 fragment 顺序。
- 已完成基础校验：`compileall`、`pyi --help`、runtime/prompt/skill 本地冒烟。

### 建议优先落地（Phase 2）

- 持久化会话级 skill/template 激活状态，避免重启进程后丢失。
- 引入独立的 context source 抽象，不再让 `context_files` 只能折叠进 system prompt。
- 增强 runtime diagnostics 的结构化输出，至少覆盖 skipped/missing fragment 的机器可读结果。
- 在不引入代码扩展的前提下，继续扩充文本资源驱动能力。

### 中期候选（Phase 3）

- 实现 compaction strategy 与 `/compact`。
- 引入 Python 代码扩展入口，但仍保持显式启用与清晰诊断。
- 支持 provider capability 感知，并据此控制 prompt/tool 的条件启用。
- 视需要引入自动文件监听，减少手动 `/reload` 成本。

### 仍待决定

- 是否将 skill/template 激活状态写入 session metadata，而不是只放在当前进程内存中。
- 是否把 `context_files` 从“拼接进 system prompt”升级为单独的上下文装配层。
- 是否需要把 prompt/template/skill 名称冲突从 warning 提升为硬错误。
- 是否需要给 `/skill:<name>` 增加停用、列出、临时/持久两种模式。
- 是否需要把 `/runtime json prompt` 进一步扩展为 provider request 级别的完整 preflight inspection。

### 暂不建议现在做

- 在当前阶段引入完整自动文件监听。
- 在当前阶段引入大而全的 extension hook 管线。
- 在当前阶段把 prompt inspection 扩展到过多非核心诊断字段，导致 `/runtime` 家族命令失去聚焦。
