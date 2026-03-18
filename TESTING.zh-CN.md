# Astra 测试与验收流程

本文档把当前仓库已经具备的功能验证流程整理成一套可重复执行的检查清单，目标覆盖：

- 包和 CLI 入口是否可用
- 配置、runtime、prompt/skill/template 能力是否正常
- agent 的 tool-calling 闭环是否正常
- 会话保存、分叉、切换、恢复是否正常
- 必要时，真实 provider 端到端调用是否正常

除非特别说明，以下命令都在仓库根目录执行：`/root/proj/astra`

## 1. 环境准备

推荐先准备本地虚拟环境并安装测试依赖：

```bash
uv venv .venv
. .venv/bin/activate
uv pip install -e ".[test]"
```

本地 smoke 和大多数自动化测试不需要真实 provider，但 CLI 启动路径要求存在 `OPENAI_API_KEY`。因此可以先设置一个假的值：

```bash
export OPENAI_API_KEY=test-key
```

如果你准备做真实 provider 端到端验证，再把它换成真实 key，或者通过仓库 `.env` 提供。

## 2. 最小冒烟验证

先跑最基础的两步：

```bash
uv run python -m compileall src
uv run python -m astra --help
```

这两步主要验证：

- Python 源码没有语法错误
- `astra` CLI 入口可以正常加载
- 参数解析和主模块导入没有明显回归

## 3. 自动化测试主线

### 快速验证

这是日常最推荐的一组：

```bash
uv run python -m pytest -q tests/unit tests/integration -m "not contract" --cov=astra --cov-fail-under=50
```

这组测试主要覆盖：

- 配置优先级、YAML 校验、默认值和 `.env` 合并
- capability runtime 的 prompt/skill/template 发现与组装
- skill/template 未激活时保持 inert 的行为
- agent 的 tool-calling 循环、snapshot restore 和 reload
- CLI 常见命令路径
- 子进程方式启动 CLI 的基本 smoke

### 单独跑 contract

如果你要确认 provider 协议解析本身没有回归，可以单独跑：

```bash
uv run python -m pytest -q tests/contract -m contract
```

这类测试当前是本地 HTTP/SSE contract，不依赖真实 OpenAI 调用。

### 更宽松但更全的本地验证

如果你不关心覆盖率门槛，或者想顺手把 contract 以外的测试都跑进去：

```bash
uv run python -m pytest -q tests/unit tests/integration -m "not contract"
```

## 4. 一键本地 smoke

仓库已经提供现成脚本：

```bash
bash scripts/smoke_cli.sh
```

这个 shell 脚本只是薄包装，实际执行的是 `scripts/smoke_cli.py`。如果你想直接跑 Python 版本，也可以用：

```bash
uv run python scripts/smoke_cli.py
```

脚本支持这些常用选项：

```bash
uv run python scripts/smoke_cli.py --skip-pytest
uv run python scripts/smoke_cli.py --live-provider
uv run python scripts/smoke_cli.py --real
uv run python scripts/smoke_cli.py --real --env-file /path/to/.env
uv run python scripts/smoke_cli.py --keep-temp
```

默认情况下它会依次执行：

- `compileall`
- `python -m astra --help`
- `pytest -q tests/unit tests/integration -m "not contract"`
- 一个脚本化 CLI 会话

脚本化 CLI 会话会覆盖这些命令路径：

- `/help`
- `/tools`
- `/skills`
- `/templates`
- `/runtime`
- `/runtime warnings`
- `/runtime json`
- `/runtime prompt`
- `/runtime json prompt`
- `/model`
- `/base-url`
- `/skill:<name>`
- `/template:<name>`
- `/reload`
- `/reload code`
- `/fork`
- `/rename`
- `/save`
- `/sessions`
- `/resume`
- `/switch`

其中真实 provider 的附加校验会读 `note.txt`，确认流式输出、tool call 发出、tool result 回灌和最终回答内容都正常。

## 5. 手工 CLI 验收

如果你刚改了 CLI、prompt assembly、session 行为，建议再手工走一遍。

### 创建手工沙箱

```bash
uv run python scripts/manual_cli.py
```

这个脚本会准备临时工作区并启动：

```bash
python -m astra --cwd <temp-workspace>
```

它会自动放好：

- `.astra/config.yaml`
- `.astra/prompts/*`
- `.astra/skills/*`
- 适合 `read`、`edit`、`find`、`grep`、`ls`、`bash` 的示例文件

### 手工命令清单

进入 CLI 后，至少建议走一遍：

```text
/help
/tools
/skills
/templates
/runtime
/runtime warnings
/runtime json
/runtime prompt
/runtime json prompt
/model
/model smoke-model
/base-url
/base-url http://gateway.local/v1
/sessions
/resume
/fork smoke-copy
/rename smoke-main
/save
/skill:review
/skill:debug
/template:repo-rules
/reload
/reload code
/exit
```

重点观察：

- `/runtime prompt` 是否准确反映默认 prompt、生成的 skill catalog、以及激活后的 template 变化
- `/runtime json prompt` 是否与 `/runtime prompt` 的内容一致，只是以机器可读形式输出
- `/skill:<name>` 和 `/template:<name>` 是否真的影响最终 assembled prompt
- `/reload` 后 model/base_url/tools/runtime summary 是否保持一致
- `fork`、`rename`、`save`、`switch`、`resume` 是否真的落盘并能恢复
- slash 命令本身不会创建空会话，只有正常用户消息才会 materialize session

## 6. 真实 provider 端到端验证

当你需要确认“不是只在 fake provider 下通过”时，再跑这一层。

最简单的做法是直接用现成 smoke 脚本：

```bash
bash scripts/smoke_cli.sh --live-provider
```

等价别名：

```bash
bash scripts/smoke_cli.sh --real
```

这个模式会先完成本地 smoke，然后再补一轮真实 provider 调用，验证：

- 流式输出
- tool call 发出
- tool result 回灌
- 最终模型回答中包含从工作区文件读到的内容

如果真实 key 不在当前 shell 环境里，脚本会优先尝试复用仓库 `.env`。也可以显式指定：

```bash
bash scripts/smoke_cli.sh --real --env-file /path/to/.env
```

## 7. 功能覆盖与推荐顺序

如果你的目标是“把当前已经有的功能都测一遍”，推荐顺序是：

1. `compileall`
2. `python -m astra --help`
3. `pytest -q tests/unit tests/integration -m "not contract"`
4. `pytest -q tests/contract -m contract`
5. `bash scripts/smoke_cli.sh`
6. 手工走一次 CLI 验收
7. 在需要时执行 `bash scripts/smoke_cli.sh --live-provider`

各层的作用分别是：

- 第 1-2 步：确认包和 CLI 没坏
- 第 3-4 步：确认核心行为、contract 解析和回归测试没坏
- 第 5 步：确认当前 CLI/runtime 面的脚本化验收没坏
- 第 6 步：确认操作者视角的体验和组合路径没坏
- 第 7 步：确认真实 provider 下的流式 + tool calling 端到端没坏

## 8. 常见失败点

- `OPENAI_API_KEY is required`
  - 说明你没有设置环境变量，也没有可用的工作区 `.env`
- `capabilities.skills.enabled has been removed`
  - 说明你还在使用旧配置；请改成通过 `capabilities.skills.paths` 和 `/skill:<name>` 发现和使用技能
- `python -m astra --help` 失败
  - 优先检查虚拟环境是否激活、是否执行过 `uv pip install -e ".[test]"`
- `smoke_cli.sh` 失败在 `/reload code`
  - 先确认你修改后的模块仍可被 `importlib.reload()` 正常导入
- `/save`、`/rename`、`/fork` 提示没有可保存会话
  - 说明你还没发过正常用户消息，或者当前会话还没被 materialize
- `--live-provider` 失败
  - 优先检查真实 key、`base_url`、模型名、网络可达性

## 9. 修改代码后的最低验证要求

建议按改动范围选择最低验证集：

- 只改文档：
  - 通常不需要运行测试
- 只改配置合并、默认值、runtime 装配：
  - `uv run python -m compileall src`
  - `uv run python -m pytest -q tests/unit tests/integration -m "not contract"`
  - 如果涉及 prompt 装配，再补 `/runtime prompt` 和 `/runtime json prompt`
- 改 CLI 命令面：
  - `uv run python -m compileall src`
  - `uv run python -m astra --help`
  - `uv run python -m pytest -q tests/unit tests/integration -m "not contract"`
  - `bash scripts/smoke_cli.sh`
- 改 prompt assembly / skill / template：
  - 上述命令外，加手工检查 `/skills`、`/templates`、`/runtime prompt` 和 `/runtime json prompt`
- 改 provider/tool-calling：
  - 上述命令外，补一轮 `pytest -q tests/contract -m contract`
  - 需要端到端再补 `bash scripts/smoke_cli.sh --live-provider`
