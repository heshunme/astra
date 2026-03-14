# Astra 测试与验收流程

本文档把当前仓库已经具备的功能验证流程落地成一套可重复执行的检查清单，目标是覆盖：

- 包和 CLI 入口是否可用
- 配置、runtime、prompt/skill/template 能力是否正常
- agent tool-calling 闭环是否正常
- 会话保存、分叉、切换是否正常
- 在需要时，真实 provider 端到端调用是否正常

除非特别说明，以下命令都在仓库根目录执行：`/root/proj/astra`

## 1. 环境准备

推荐先准备本地虚拟环境并安装测试依赖：

```bash
uv venv .venv
. .venv/bin/activate
uv pip install -e ".[test]"
```

本地 smoke 和绝大多数自动化测试不需要真实 provider 调用，但 CLI 启动路径要求存在 `OPENAI_API_KEY`。因此可以先设置一个假的值：

```bash
export OPENAI_API_KEY=test-key
```

如果你准备做真实 provider 端到端验证，再把它换成真实 key，或者通过仓库 `.env` 提供。

## 2. 最小冒烟验证

先跑最基础的两步：

```bash
.venv/bin/python -m compileall src
.venv/bin/python -m astra --help
```

这两步主要验证：

- Python 源码没有语法错误
- `astra` CLI 入口可以正常加载
- 参数解析和主模块导入没有明显回归

## 3. 自动化测试主线

### 快速验证

这是日常最推荐的一组：

```bash
.venv/bin/python -m pytest -q tests/unit tests/integration -m "not slow and not contract" --cov=astra --cov-fail-under=50
```

这组测试主要覆盖：

- 配置优先级、YAML 校验、默认值
- capability runtime 的 prompt/skill/template 发现与组装
- skill/template 未激活时保持 inert 的行为
- agent 的 tool calling 循环
- CLI 常见命令路径
- 子进程方式启动 CLI 的基本 smoke

### 更宽松但更全的本地验证

如果你不关心覆盖率门槛，或者想顺手把慢一些的集成也跑进去：

```bash
.venv/bin/python -m pytest -q tests/unit tests/integration -m "not contract"
```

### 扩展验证

如果要单独检查慢测试或 contract 测试：

```bash
.venv/bin/python -m pytest -q tests/integration/test_cli_smoke_subprocess.py
.venv/bin/python -m pytest -q -m "contract or slow"
```

说明：

- `contract` 是否需要真实网络，取决于测试实现和你本地环境
- 在默认开发流程里，`unit + integration` 已经是当前项目最有价值的一层

## 4. 一键本地 smoke

仓库已经提供现成脚本：

```bash
bash scripts/smoke_cli.sh
```

这会依次执行：

- `compileall`
- `python -m astra --help`
- `pytest -q tests/unit tests/integration -m "not contract"`
- 一个脚本化 CLI 会话

脚本化 CLI 会话会覆盖这些命令路径：

- `/help`
- `/tools`
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

这个脚本默认使用临时工作区和假的 `OPENAI_API_KEY=test-key`，不会要求真实 provider。

## 5. 手工 CLI 验收

如果你刚改了 CLI、prompt assembly、session 行为，建议再手工走一遍。

### 创建手工沙箱

```bash
.venv/bin/python scripts/manual_cli.py
```

这个脚本会准备临时工作区并启动：

```bash
python -m astra --cwd <temp-workspace>
```

它会自动放好：

- `.astra/config.yaml`
- `.astra/prompts/*`
- `.astra/skills/*`
- 用于 `read`、`edit`、`find`、`grep`、`ls`、`bash` 的示例文件

### 手工命令清单

进入 CLI 后，至少建议走一遍：

```text
/help
/tools
/runtime
/runtime warnings
/runtime prompt
/runtime json
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
/template:repo-rules
/runtime prompt
/reload
/exit
```

重点观察：

- `/runtime prompt` 是否准确反映激活前后的 prompt 变化
- `/skill:` 和 `/template:` 是否真的影响最终 assembled prompt
- `/reload` 后 model/base_url/tools/runtime summary 是否保持一致
- `fork`、`rename`、`save`、`switch`、`resume` 是否真的落盘并能恢复

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

这个模式会在本地 smoke 之后，再补一轮真实 provider 调用，验证：

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
4. `bash scripts/smoke_cli.sh`
5. 手工走一次 CLI 验收
6. 在需要时执行 `bash scripts/smoke_cli.sh --live-provider`

各层的作用分别是：

- 第 1-2 步：确认包和 CLI 没坏
- 第 3 步：确认核心行为和回归测试没坏
- 第 4 步：确认当前 CLI/runtime 面的脚本化验收没坏
- 第 5 步：确认操作者视角的体验和组合路径没坏
- 第 6 步：确认真实 provider 下的流式 + tool calling 端到端没坏

## 8. 常见失败点

- `OPENAI_API_KEY is required`
  - 说明你没有设置环境变量，也没有可用的工作区 `.env`
- `python -m astra --help` 失败
  - 优先检查虚拟环境是否激活、是否执行过 `uv pip install -e ".[test]"`
- `smoke_cli.sh` 失败在 `/reload code`
  - 先确认你修改后的模块仍可被 `importlib.reload()` 正常导入
- `--live-provider` 失败
  - 优先检查真实 key、`base_url`、模型名、网络可达性

## 9. 修改代码后的最低验证要求

建议按改动范围选择最低验证集：

- 只改文档：
  - 通常不需要运行测试
- 只改配置合并、默认值、runtime 装配：
  - `compileall`
  - `pytest -q tests/unit tests/integration -m "not contract"`
- 改 CLI 命令面：
  - `compileall`
  - `python -m astra --help`
  - `pytest -q tests/unit tests/integration -m "not contract"`
  - `bash scripts/smoke_cli.sh`
- 改 prompt assembly / skill / template：
  - 上述命令外，加手工检查 `/runtime prompt` 和 `/runtime json prompt`
- 改 provider/tool-calling：
  - 上述命令外，补一轮 `bash scripts/smoke_cli.sh --live-provider`

这份文档的目标不是替代测试，而是把“当前仓库已有能力怎么验收”固定下来，减少每次手工回忆流程的成本。
