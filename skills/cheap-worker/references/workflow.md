# 命令与任务契约

`worker` 下文指 `python3 <本技能目录>/scripts/worker.py`。路径相对于目标 Git 仓库。
框架的默认配置与 `.env` 来自技能链接所指向的 `multi_model_codex`，与当前工作目录无关。
全局选项 `--config`、`--env-file`、`--runs-dir` 放在子命令前面。

## 配置

`worker doctor` 只检查配置，不联网。仅需框架 `.env` 中：

```dotenv
EXECUTOR_BASE_URL=https://provider.example/v1
EXECUTOR_API_KEY=实际执行模型密钥
EXECUTOR_MODEL=供应商模型标识
```

协议在框架 `config.yaml` 的 `roles.executor.api` 设置，支持 `chat_completions` 和 `responses`。
Chat 供应商的 token 参数可选 `max_tokens` / `max_completion_tokens`。
`worker doctor --probe` 发起两次真实 API 请求，检查工具调用兼容性；不要将它当离线检查。

## 首轮执行

由聊天模型写 UTF-8 JSON 文件，保存在目标仓库以外，避免混进源码：

```json
{
  "task": "修正 add(a,b) 并添加有意义的回归测试",
  "context": "保持现有函数接口。此处补充必要设计决定和适用的外层仓库指令。",
  "writable_paths": ["calc.py", "tests/test_calc.py"],
  "acceptance_criteria": ["add(2,3) 返回 5，并保留原有测试"],
  "checks": {
    "unit": {"argv": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]}
  }
}
```

`writable_paths` 仅允许精确文件或以 `/` 结尾的目录前缀，如 `src/component/`。不支持通配符、`.` 或绝对路径；优先使用具体文件。
`checks` 是由你确定的完整 argv，不使用 shell 拼接；不能覆盖全局固定检查。至少包含一个有实际意义的测试/构建/内容检查。不要把示例 Python 命令直接用于不适用的 ROS/C++ 项目。
检查子进程不继承 API key、HOME、PYTHONPATH 或 ROS 环境。ROS 项目需要你提供经过审阅的检查包装脚本，显式 source 正确环境，再将脚本作为固定 argv；这不会自动安装依赖。

```bash
worker push --repo /absolute/project
worker execute --push-before --repo /absolute/project --contract /outside/project/contract.json
```

`push` 要求当前分支有 upstream、工作区干净，且只执行普通 `git push`。`--push-before` 将同一操作作为 execute 的第一步；这是外部写操作，只有用户已授权目标 remote 时才使用。复制模式允许未提交修改，因此此时 push 会拒绝并明确说明未完成备份。

输出 `Run: ...`，记下运行目录。默认复制工作区在 `<run>/worktree`，复制时排除 `.git`、凭据、虚拟环境和运行日志；源项目此时不被修改。复制品内部建立一次基线提交，仅用于生成相对当前用户工作区的候选补丁，不会推送。
如 `config.yaml` 设置 `execution.mode: branch`，则使用 detached worktree，要求源仓库干净，也不会创建命名分支。默认检查还包含 `git diff --check`。

## 复核与返工

```bash
worker inspect --run /absolute/run
worker rework --run /absolute/run --feedback-file /outside/project/feedback.txt
```

`inspect` 是离线操作，显示完整 diff、当前摘要、`verification_current` 和状态。
另外阅读 `<run>/candidate.patch`、`<run>/<latest_round>/executor-report.txt`、`verification.json` 和对应 `check_*.log`。
失败或中断时补丁可能是旧轮次的；只允许验收当前 READY_FOR_REVIEW 状态和匹配摘要。
`rework` 保留同一工作区、契约、检查目录与累计预算，只追加聊天模型反馈。
修改契约应拆成新任务，由聊天模型重新确定范围；不要让执行器改变自己的验收标准。
外部直接改过工作区时，旧验证失效；应重新验证并复核，可用一次带具体反馈的返工轮次。

## 验收及应用

由聊天模型在阅读证据后写 review JSON：

```json
{
  "snapshot_digest": "复制 inspect 中与验证一致的 64 位摘要",
  "decision": "PASS",
  "summary": "实现保留接口，新增测试覆盖修复行为，独立检查通过",
  "findings": [],
  "criteria_status": [
    {
      "criterion": "add(2,3) 返回 5，并保留原有测试",
      "status": "PASS",
      "evidence": "calc.py 使用加法；实际 unit 日志中新增测试和原有测试均通过"
    }
  ]
}
```

每个原始标准必须恰好出现一次且有证据。存在未解决的 critical/high/medium 问题时不能验收。
记录字段是聊天模型的审阅声明，脚本无法证明提交声明的是哪一种模型；不要交给执行器填写。

```bash
worker apply --run /absolute/run --review-file /outside/project/review.json
worker rollback --run /absolute/run
```

该命令不调用 API。`apply` 校验独立检查、审阅标准、最新内容摘要、补丁和源仓库 HEAD/工作区状态未变化，再 `git apply --check` / `git apply`。修改保留为未提交状态，并记录应用后的状态指纹。`rollback` 只接受 `APPLIED` 运行，验证源仓库仍等于应用后状态，再用同一候选补丁执行反向检查和恢复；应用后源文件发生任何变化都会拒绝自动恢复。
如果源仓库在执行期间发生变化，停止自动应用，由你基于最新内容集成并重新验证；不要强制覆盖。

状态 `READY_FOR_REVIEW` 返回 0，检查失败/执行未完成返回 3，运行错误返回 1，前置检查或无效操作返回 2，中断返回 130。
配置预算以一次运行及其返工为单位；默认 SDK 自动重试关闭，避免隐含放大请求数。开启后重试 HTTP 次数不计入逻辑模型请求数。
