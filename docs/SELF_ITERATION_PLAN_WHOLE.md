• 按方向（自主改代码 + 安全优先），这 7 类基础设施的当前状态如下（截至 2026-03-09）：
 
  1. 变更安全壳（P0）【部分完成】
 
  - [x] 自动 checkpoint（当前为工作区文件快照，不是分支/提交快照）。
  - [x] 失败自动回滚（attempt 失败、gate 失败、异常都会回滚）。
  - [x] 脏工作区保护（dirty worktree 会 fail-fast）。
  - [ ] 分支/提交级 checkpoint（git commit/branch 级别）尚未实现。
 
  2. 机器可判定的验收门禁（P0）【已完成（Phase 1）】
 
  - [x] 固化 gate：`compileall` + `pytest -q tests/unit` + `astra --help`。
  - [x] 统一 gate 判定与 stop-on-first-failure。
  - [x] gate 失败会触发自动回滚并记录结果。
 
  3. 任务基准与回归集（P0）【部分完成】
 
  - [x] 固定 YAML 任务集（`.astra/benchmarks/tasks.yaml`）已建立。
  - [x] `/iterate benchmark [path]` 与分数板（accept_rate/avg_score/avg_duration）已建立。
  - [ ] 面向“期望输出”的细粒度判分（任务级预期断言）尚未建立。
 
  4. 迭代控制器（P1）【已完成（Phase 2）】
 
  - [x] 单次状态机已具备：preflight -> checkpoint -> attempt -> validate -> accept/revert -> record。
  - [x] 多步闭环控制已具备（最大步数/预算/失败次数，`/iterate auto`）。
 
  5. 结构化观测与实验账本（P1）【部分完成】
 
  - [x] JSONL 账本已落地（run_id、decision、score、changed_files、gate_results、failure_class、duration/error）。
  - [ ] token 消耗、策略实验维度、完整命令执行轨迹仍不完整。
 
  6. 失败分类与策略切换（P1）【部分完成】
 
  - [x] 失败分类已支持：syntax/test/cli/env/timeout/unknown。
  - [ ] 策略切换（如缩小改动、换路径重试）尚未实现，当前主要策略为回滚。
 
  7. 受控执行沙箱（P2）【未完成】
 
  - [ ] 临时 worktree/隔离目录执行尚未实现。
  - [ ] 当前仍是 workspace-scoped 工具执行，不是独立实验沙箱。
