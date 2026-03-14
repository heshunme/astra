# 请求流程（基于当前代码）

本文描述 `python -m astra` 在当前仓库中的真实请求链路，重点是普通用户请求和工具循环。

## 1. 启动与运行时装配

1. CLI 解析参数：`--model`、`--base-url`、`--cwd`、`--session`、`--new-session`、`--system-prompt`、位置参数 `prompt`。  
2. 读取环境：进程环境优先，`<cwd>/.env` 只补缺失变量（`setdefault` 语义）。  
3. 读取配置：合并 `~/.astra-python/config.yaml` 和 `<cwd>/.astra/config.yaml`，项目覆盖全局。  
4. 解析运行时配置：  
   - `model = CLI > YAML > OPENAI_MODEL > 默认值`  
   - `base_url = CLI > YAML > OPENAI_BASE_URL > 默认值`  
   - `system_prompt = CLI > YAML > ""`  
5. 检查 `OPENAI_API_KEY`，缺失即退出。  
6. 初始化 `CapabilityRuntime`、`Agent`、`SessionStore`。  
7. 如果使用 `--session`（且未 `--new-session`），先加载旧会话，再用会话里的 `model/system_prompt/cwd` 覆盖当前 agent 配置。  
8. 启动时执行一次 `agent.reload_runtime(...)`，构建工具与 prompt 快照。

## 2. Runtime reload 产物

`CapabilityRuntime.reload()` 会：

1. 注册并筛选启用工具：`read/write/edit/ls/find/grep/bash`。  
2. 注册基础 prompt 片段：  
   - `builtin:base`（默认系统提示）  
   - `config:system`（运行时 `system_prompt`）  
3. 扫描并加载 prompt 文件：  
   - `~/.astra-python/prompts/*.md`  
   - `<cwd>/.astra/prompts/*.md`  
   - `capabilities.prompts.paths`  
   每个文件映射为 `prompt:<stem>`。  
4. 扫描并加载 skill：  
   - `~/.astra-python/skills/*/skill.yaml`  
   - `<cwd>/.astra/skills/*/skill.yaml`  
   - `capabilities.skills.paths`  
   仅做发现与索引，不自动把 skill 正文注入系统提示。  
5. 生成 diagnostics（加载结果与 warning），并更新 runtime snapshot。  

说明：`capabilities.skills.enabled` 已删除，配置里出现会报错。

## 3. 输入分流

REPL 模式每次读入一行后：

1. 若以 `/` 开头且命中命令注册表，走命令处理，不进入模型请求。  
2. 否则作为普通请求进入 `run_user_prompt()`，再调用 `agent.prompt(...)`。  

一次性调用模式（命令行直接传 `prompt`）会直接 `agent.prompt(...)`。

## 4. 普通请求主链路

`Agent.prompt(text)` 执行顺序：

1. materialize 会话（从“未落地会话”变为可保存会话）。  
2. 若是首条普通消息且会话无名，用该消息设默认会话名。  
3. 把用户消息追加到 `session.messages`（可带 metadata）。  
4. 先保存会话到 `~/.astra-python/sessions/<id>.json`。  
5. 进入 `_run()` 开始 provider 循环。

## 5. `_run()` 中的 provider + tools 循环

每一轮 `_run()`：

1. 组装 provider messages：  
   - 首条为当前 assembled system prompt（如果非空）  
   - 再附加历史 `user` / `assistant` / `tool_result`  
2. 组装可用工具 schema（OpenAI function calling 结构）。  
3. 调用 OpenAI-compatible `/chat/completions`，SSE 流式读取事件。  
4. 流式过程中累计：  
   - 文本增量 `text_delta`  
   - 工具调用增量 `tool_call_delta`（按 index 聚合 id/name/arguments）  
   - `usage`  
5. 轮次结束后写入一条 assistant 消息。  
6. 若无 tool calls：保存会话并返回。  
7. 若有 tool calls：逐个本地执行并写入 `tool_result` 消息，再回到下一轮 provider 请求。

这就是标准闭环：`assistant(tool_calls) -> tool_result -> assistant -> ...`，直到不再调用工具。

## 6. 工具执行与安全边界

1. 文件类工具通过 `resolve_workspace_path()` 做路径约束，禁止逃逸 workspace。  
2. `read` 仅支持 UTF-8 文本，支持行范围与最大行数。  
3. `bash` 在 `ctx.cwd` 执行，带超时与输出上限；超长输出截断并写临时日志文件。  
4. `find/grep` 会跳过常见大目录（如 `.git`、`.venv`、`node_modules`）。  
5. 所有工具结果统一格式：`OK\n...` 或 `ERROR\n...`。

## 7. Prompt 组装（与请求强相关）

`Agent.inspect_prompt()` 是当前实际 system prompt 的唯一组装路径：

1. 先取 runtime 默认片段顺序（`prompts.order`）。  
2. 再注入“session skill catalog”文本（仅目录和说明，不含 skill 正文）。  
3. 再拼接当前会话已激活 template（`/template:<name>`）。  
4. 去重后用空行拼接，结果写入 `current_system_prompt`。  

`/runtime prompt` 与 `/runtime json prompt` 都复用这条组装链路。

## 8. Skill 与 template 在请求前的改写

1. `/skill:<name> <request>`：立即改写成普通用户文本后发起本轮请求。  
2. `/skill:<name>`：仅“武装”下一条普通输入，消费一次后清除。  
3. `/template:<name>`：激活模板 prompt 片段，影响后续请求的 system prompt。  
4. skill 触发依赖 `read` 工具；若 `read` 被禁用，则 `/skill:` 返回错误提示。

## 9. 会话持久化关键点

1. 普通用户消息在调用 provider 之前就先落盘。  
2. `/help`、`/runtime`、`/tools` 等 slash 命令本身不会自动创建新会话文件。  
3. `reload/switch/resume/fork/rename/save` 都围绕同一套 session JSON 存储。  
4. session 除消息外还保存 `model/system_prompt/cwd` 与 skill catalog 快照。  

## 10. 运行时限制

1. 流式响应期间禁止 `/reload` 与 `/reload code`。  
2. `/reload code` 仅 best-effort，稳定路径仍是 `/reload`。  
3. provider 中止时返回 `Request aborted`，不会继续工具循环。
