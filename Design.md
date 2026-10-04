# 设计文档（Design.md）

> 项目：代码审查 Agent（LangGraph 版）
> 提交人：2412190209 王宝硕
> 配套说明：功能与使用方法见 `README.md`

---

## 1. 背景与目标

### 1.1 要解决的问题

人工代码审查有两个痛点：一是**遗漏**（人容易漏看边界条件、异常处理），二是**重复劳动**
（读文件、数函数、跑一小段代码验证猜想，这些动作机械且耗时）。

本项目把这三个机械动作交给工具，把「判断哪里有问题、为什么有问题」留给 LLM：

- 读取代码 → `file_reader`
- 看清结构（函数列表、圈复杂度、可疑写法）→ `ast_analyzer`
- 验证猜想（这段代码到底会不会崩）→ `code_executor`

### 1.2 设计目标

| 优先级 | 目标 | 落地方式 |
|---|---|---|
| P0 | 用 LangGraph 表达 Agent 循环 | `agent/graph.py` 的 `StateGraph` |
| P0 | 工具调用闭环可靠 | `ToolNode` + 条件边 + 迭代上限 |
| P0 | 不把本机密钥/文件暴露给远端模型 | 目录白名单 + 敏感文件拦截 + 环境变量裁剪 |
| P1 | 过程可见、可调试 | 流式输出工具轨迹 + `--graph` 导出结构图 |
| P1 | 可测试 | 依赖注入（可换假模型）+ 纯标准库测试 |

### 1.3 非目标

- 不做生产级的不可信代码执行（见第 5 节安全模型）
- 不做多文件 / 仓库级 / diff 级审查
- 不做 CI 门禁集成

---

## 2. 总体架构

```text
┌──────────────────────────────────────────────┐
│                   cli.py                     │
│  参数解析 / 交互循环 / 流式渲染 / 异常降级    │
└───────────────┬──────────────────────────────┘
                │ build_code_review_agent()
┌───────────────▼──────────────────────────────┐
│                agent/                        │
│  graph.py  图编排：节点、条件边、记忆、裁剪   │
│  prompt.py 系统提示词（纯文本，无逻辑）       │
└───────────────┬──────────────────────────────┘
                │ get_all_tools()
┌───────────────▼──────────────────────────────┐
│                tools/                        │
│  file_reader    读文件（白名单 + 敏感拦截）   │
│  ast_analyzer   静态分析（结构/复杂度/问题）  │
│  code_executor  受限执行（隔离目录 + 策略）   │
│  sandbox_policy AST 策略校验                  │
└───────────────┬──────────────────────────────┘
                │
┌───────────────▼──────────────────────────────┐
│               config.py                      │
│  集中配置：LLM、路径边界、执行限制、运行时    │
└──────────────────────────────────────────────┘
```

**分层原则**：上层只管编排，下层只管单一能力，配置只在一处。
`cli.py` 不直接调用任何工具，`tools/` 不感知图的存在，`config.py` 不依赖任何业务模块——
因此每一层都可以独立替换和测试。

---

## 3. LangGraph 图设计

### 3.1 图结构

```text
        START
          │
          ▼
     ┌─────────┐  无 tool_calls   ┌───────┐
     │  agent  │ ───────────────► │  END  │
     └────┬────┘                  └───────┘
          │ 有 tool_calls
          ▼
     ┌─────────┐
     │  tools  │
     └────┬────┘
          │
          └──────────► 回到 agent
```

### 3.2 节点与边

| 元素 | 类型 | 职责 |
|---|---|---|
| `agent` | 节点 | 拼装 `[SystemMessage] + 裁剪后的历史`，调用绑定了工具的模型 |
| `tools` | 节点 | 使用 LangGraph 内置 `ToolNode`，自动执行消息中的全部 `tool_calls` |
| `should_continue` | 条件边函数 | 检查最后一条消息有无 `tool_calls`，返回 `"tools"` 或 `"end"` |
| `START → agent` | 普通边 | 入口固定先问模型 |
| `tools → agent` | 普通边 | 工具结果回填后继续推理，形成 ReAct 闭环 |

### 3.3 为什么用内置 `ToolNode`

`ToolNode` 已经处理了「一条消息里多个 `tool_calls`」「未知工具名」「参数不符合 schema」
「异常转成 `ToolMessage`」等细节，自己实现这些分支既啰嗦又容易出错。

### 3.4 为什么条件边返回字符串而不是布尔值

图的分支语义显式写在映射表里：

```python
workflow.add_conditional_edges(
    "agent",
    should_continue,
    {"tools": "tools", "end": END},
)
```

比 `{True: "tools", False: END}` 更易读，将来增加分支（例如插入一个
`summarize` 节点）只需要往映射表里加一项。

### 3.5 迭代上限

条件边只判断「要不要调工具」，本身没有终止保证——如果模型反复要求调工具，
循环会一直转下去。因此显式设置 `recursion_limit`（默认 25，见 `config.RECURSION_LIMIT`），
并在 `cli.py` 捕获 `GraphRecursionError` 转成中文提示，而不是把英文堆栈丢给用户。

---

## 4. 状态与记忆设计

### 4.1 状态选型

直接使用 LangGraph 内置的 `MessagesState`（本质是带 `messages: list[BaseMessage]`
的 `TypedDict`，并自带 `add_messages` 归约器），不再自定义状态类。
好处是零样板代码，且与 `ToolNode`、checkpointer 天然兼容。

### 4.2 会话记忆

```python
checkpointer = MemorySaver()          # agent/graph.py: create_checkpointer()
app = build_code_review_agent(checkpointer=checkpointer)
run_config = build_run_config(thread_id="abcd1234")   # configurable.thread_id
```

开启后，CLI **每轮只投递新的 `HumanMessage`**，历史由 checkpointer 维护：
这才是「框架自动管理对话记忆」的正确用法。
`clear` 命令的实现就是换一个新的 `thread_id`，而不是手动清空列表。

### 4.3 上下文裁剪

长会话会让 token 成本线性上涨，因此送进模型前先裁剪：

```python
def trim_history(messages, limit=None):
    tail = messages[-limit:]
    # 关键：截断点向前对齐到最近的 HumanMessage
    start = next((i for i, m in enumerate(tail) if isinstance(m, HumanMessage)), None)
    ...
```

**为什么必须对齐**：OpenAI 兼容接口要求每个 `ToolMessage` 都能找到对应的
`AIMessage.tool_calls`。如果粗暴地保留「最后 N 条」，很可能把
`AIMessage(tool_calls)` 与 `ToolMessage` 从中间切开，接口会直接返回 400。
从 `HumanMessage` 处开始截断可以从结构上杜绝这种非法序列。

注意裁剪只影响**送进模型的消息**，不改写图状态，因此不会破坏 checkpointer 里的完整历史。

---

## 5. 工具设计

### 5.1 统一契约：错误即返回值

三个工具都不抛异常，而是返回以「错误：」开头的中文说明。原因是**工具的输出是给模型看的**：

- 抛异常 → 图的执行中断，用户看到一个无意义的报错
- 返回错误字符串 → 模型读到「文件不存在」，可以自己改路径重试

这也是 `file_reader` 会把「允许的目录」「用 `--root` 放开」写进错误信息的原因：
给模型**可执行的下一步**，而不是只给一个失败信号。

### 5.2 `file_reader`

处理顺序有意为之：**先判敏感文件名，再判白名单**。
如果顺序反过来，攻击者可以用错误信息差异来探测白名单外是否存在某个文件。

`Path.resolve()` 之后再比对白名单，因此符号链接指向白名单外时同样会被拒绝。

### 5.3 `ast_analyzer`

输出三段：代码结构（函数/类，区分模块级函数与类方法）、圈复杂度、常见问题。
复杂度计算做了两点修正：

1. 把 `AsyncFunctionDef`、`IfExp`、推导式计入；
2. **嵌套函数的复杂度单独统计**，不再通过 `ast.walk` 重复累加到外层函数
   （`tests/test_ast_analyzer.py::test_nested_function_complexity_is_not_double_counted` 锁住这个行为）。

### 5.4 `code_executor`

执行流程：

```text
check_code_policy(code)          # 1. AST 策略校验，不通过直接返回原因
  → mkdtemp()                    # 2. 全新临时目录作为 cwd
  → write snippet.py
  → Popen([python, "-I", ...],   # 3. -I 隔离模式，忽略 PYTHONPATH / user site
          env=build_child_env(), #    只透传系统变量，密钥不进子进程
          stdin=DEVNULL)
  → communicate(timeout=10)      # 4. 超时则杀进程树后返回友好提示
  → rmtree(workdir)              # 5. 无论成功失败都清理
```

`-I`（isolated）模式值得单独说明：它同时等价于 `-E`（忽略 `PYTHONPATH` 等环境变量）
和 `-s`（不加载用户 site-packages），可以挡掉「通过环境变量注入 `sitecustomize.py`」
这条常见逃逸路径。

---

## 6. 安全模型

### 6.1 威胁建模

| # | 威胁 | 攻击路径 | 现有缓解 | 残余风险 |
|---|---|---|---|---|
| 1 | 任意文件读取 | 提示注入让模型读 `~/.ssh/id_rsa` | 目录白名单 + 敏感文件名单 | 白名单目录**内部**的业务密钥文件仍可被读取 |
| 2 | 密钥外泄给模型厂商 | 读 `.env` 后随对话发往远端 | `.env` 文件名硬拦截 | 模型服务侧的数据留存策略 |
| 3 | 子进程窃取密钥 | 执行代码读 `os.environ["LLM_API_KEY"]` | `import os` 被策略拦截 + 子进程环境变量裁剪（**双重**） | 无 |
| 4 | 任意代码执行 | 让 `code_executor` 跑 `os.system` | AST 策略校验 + 双下划线穿透拦截 | 纯 Python 层策略，无法防御 CPython 解释器漏洞 |
| 5 | 资源耗尽 | fork 炸弹 / 死循环 / 大内存 | 10 秒超时 + 杀进程树 | **无内存与 CPU 配额** |
| 6 | 工具死循环 | 模型反复要求调工具 | `recursion_limit` | 上限内仍会消耗 token |
| 7 | 路径穿越 | 软链接指向白名单外 | `resolve()` 后再比对白名单 | 无 |
| 8 | 上下文溢出 | 超长代码 + 超长会话 | `MAX_FILE_CHARS` 截断 + 历史裁剪 | 没有 token 级预算，只有条数级 |

### 6.2 为什么用 AST 而不是字符串黑名单

最初的实现是：

```python
dangerous = ["os.system", "subprocess.call", "shutil.rmtree", "os.remove"]
for d in dangerous:
    if d in code:
        return "拒绝执行"
```

这种方式同时存在**假阳性**和**假阴性**：

- 假阳性：注释 `# os.system` 就被拒绝（`test_comment_mentioning_danger_is_allowed` 锁住这个回归）
- 假阴性：`__import__("os").system(...)`、`getattr(os, "remove")(...)` 全部绕过

改成遍历语法树后：导入检查的是**顶层包名白名单**，调用检查的是**函数/属性名集合**，
属性与名字还额外拒绝双下划线形式（挡住 `(1).__class__.__bases__` 这类对象图穿透）。

同时刻意保持denied 属性集合**很窄**——只包含 `system`/`popen`/`fork`/`getenv` 等
没有正常用途的名字，避免误杀 `list.remove()`、`json.loads()` 这类日常写法
（`test_normal_attribute_calls_are_allowed` 锁住这个回归）。

### 6.3 明确声明边界

`sandbox_policy` 是**策略检查**（policy），不是**安全边界**（security boundary）。
它降低的是「LLM 顺手写出危险代码」的概率，不能抵御有明确攻击意图的对手。
真正需要执行不可信代码时，必须使用容器、虚拟机或低权限用户等操作系统级隔离。

这段声明同时写在 `tools/sandbox_policy.py` 的模块 docstring 和 README 里，避免误用。

---

## 7. 配置管理

所有魔数集中在 `config.py` 一处，分五组：LLM、Agent 运行时、文件读取、代码执行、路径边界。
每一项都是「环境变量 → 默认值」的形式。

这样做的收益在测试里最明显：测试可以临时改 `config.MAX_FILE_CHARS = 10`
来验证截断逻辑，而不需要真的造一个 20000 字符的文件。

> 实现细节：工具模块使用 `import config` 后以 `config.XXX` 方式取值，
> 而不是 `from config import XXX`。后者会把值在导入时固化，导致测试中的猴子补丁失效。

---

## 8. 错误处理与降级

| 层 | 错误 | 处理 |
|---|---|---|
| 工具层 | 文件不存在 / 越界 / 编码错 / 执行超时 | 返回中文错误字符串，交给模型自我纠正 |
| 图层 | 工具死循环 | `GraphRecursionError` 抛出 |
| CLI 层 | 上述 Recursion 错误 | 捕获并提示「拆小问题」或调大 `AGENT_RECURSION_LIMIT` |
| CLI 层 | 用户 `Ctrl+C` | 只中断本轮，不退出程序 |
| CLI 层 | 未知异常 | 打印类型与信息，回到输入循环，不崩溃 |
| CLI 层 | 输出为空（模型只调了工具没给结论） | 提示用户补充一句，而不是渲染空面板 |
| 启动 | 未配置 API Key | 启动前检查并给出配置指引，退出码 1 |
| 启动 | 初始化失败 | 打印异常类型与信息，退出码 1 |

分层原则：**工具层不抛异常、CLI 层绝不为预期错误崩溃**。

---

## 9. 测试策略

### 9.1 关键决策：依赖注入

`build_code_review_agent(llm=..., tools=..., checkpointer=...)` 三个参数都可注入。
这让图相关的测试可以塞入脚本化假模型，从而做到**不需要 API Key、不联网、不花钱**。

### 9.2 为什么不用 `GenericFakeChatModel`

LangChain 自带的 `GenericFakeChatModel` 没有实现 `bind_tools`（直接 `NotImplementedError`），
无法用于本项目的链路。因此测试里写了一个 20 行的 `ScriptedChatModel`：
按脚本依次返回预设消息，`bind_tools` 返回自身。

### 9.3 测试分层

| 层次 | 文件 | 特点 |
|---|---|---|
| 纯函数 | `test_sandbox_policy.py`、`test_ast_analyzer.py` | 无 IO、无 mock，最快 |
| 文件 IO | `test_file_reader.py` | 用 `TemporaryDirectory` 隔离，动态注册白名单根，`tearDown` 还原全局配置 |
| 子进程 | `test_code_executor.py` | 真实起进程，覆盖超时与杀进程树 |
| 图编排 | `test_graph.py` | 条件边、裁剪、结构、工具闭环、迭代上限、跨轮记忆 |

### 9.4 回归测试

两个「曾经踩过的坑」被固化成用例，防止改回去：

- 注释里出现危险字符串不应被拒绝（字符串黑名单时代误杀）
- `list.remove()` / `json.loads()` 不应被拒绝（denied 属性集合过宽时误杀）

合计 **55 个用例，全部通过**：

```text
Ran 55 tests in 2.53s

OK
```

---

## 10. 关键设计决策汇总

| # | 决策 | 备选方案 | 选择理由 |
|---|---|---|---|
| 1 | 用内置 `MessagesState` | 自定义 `TypedDict` 状态 | 零样板，且与 `ToolNode`/checkpointer 天然兼容 |
| 2 | 用 `ToolNode` | 手写工具执行节点 | 多调用、未知工具、异常转消息等分支已被覆盖 |
| 3 | 记忆交给 checkpointer | CLI 手动维护消息列表 | 让框架能力真正落地，`clear` 只需换 `thread_id` |
| 4 | 工具错误用返回值 | 抛异常 | 模型的纠错能力依赖于能读到失败原因 |
| 5 | 执行策略用 AST | 危险字符串黑名单 | 同时消除假阳性与假阴性 |
| 6 | LLM 客户端模块级缓存 | 每次进节点新建 | 节点在工具回环中会被反复重入，新建会浪费连接池 |
| 7 | 配置集中 + 运行时取值 | 各模块 from-import 常量 | 集中便于审计，运行时取值才能被测试覆盖 |
| 8 | 提示词外置为 `prompt.py` | 写在 `graph.py` 里 | 提示词迭代频率高，应与编排逻辑解耦 |
| 9 | 测试用 `unittest` | `pytest` | 零额外依赖，`python -m unittest` 即可跑通 |

---

## 11. 已知限制与演进路线

### 11.1 限制

1. `MemorySaver` 只存在内存，进程退出即丢失，也没有多进程共享
2. 只能审查单文件，不支持目录 / 多文件 / diff / PR
3. 报告是自然语言，没有结构化的 `findings`，无法直接做 CI 门禁
4. 无内存与 CPU 配额，超时只能防「挂死」，不能防「吃满内存」
5. 无 token 级预算控制，只有消息条数级裁剪
6. 主要在 Windows 上验证，POSIX 分支未做长期测试

### 11.2 演进路线

| 优先级 | 方向 | 具体做法 |
|---|---|---|
| 高 | 结构化报告 | 扩展状态增加 `findings: list[Finding]`，新增 `report` 节点产出 JSON，CLI 支持 `--format json` |
| 高 | 真沙箱 | `code_executor` 后端可插拔，接入 Docker / `nsjail` / Windows Job Object |
| 中 | 资源配额 | 子进程设置内存上限（`resource.setrlimit` / Job Object） |
| 中 | token 预算 | 用 `trim_messages` + 真实 token 计数替换条数裁剪 |
| 中 | 持久化记忆 | 把 `MemorySaver` 换成 `SqliteSaver` / `PostgresSaver` |
| 低 | 多文件审查 | 增加 `list_files` / `grep` 工具，支持目录级审查 |
| 低 | 可观测性 | 接入 LangSmith 或 OpenTelemetry，记录每步耗时与 token |
