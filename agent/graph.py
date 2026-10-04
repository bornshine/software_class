"""LangGraph 版代码审查 Agent 核心。

图结构：
    START → agent(调 LLM) → [条件边] ── 有 tool_calls ──→ tools → agent → …
                                      └─ 无 tool_calls ──→ END

相比最初版本修复的工程问题：
1. LLM 客户端只构建一次（模块级 lru_cache），不再每次进节点都新建一个客户端；
2. 显式设置 recursion_limit，避免工具调用无限循环，CLI 层友好处理超限；
3. 支持 checkpointer + thread_id，让「框架自动管理对话记忆」真正成立；
4. 送进模型前裁剪历史，避免长会话 token 成本线性膨胀；
5. 裁剪从 HumanMessage 开始，绝不切断 tool_call 与 ToolMessage 的配对。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState
from langgraph.prebuilt import ToolNode

import config
from agent.prompt import SYSTEM_PROMPT
from tools import get_all_tools

NodeName = Literal["tools", "end"]


@lru_cache(maxsize=1)
def get_llm() -> BaseChatModel:
    """构建 LLM 客户端（进程内只建一次，复用底层连接池）。"""
    if not config.api_key_configured():
        raise RuntimeError(
            "未配置 LLM_API_KEY：请复制 .env.example 为 .env 并填入你的密钥。"
        )
    return ChatOpenAI(
        model=config.LLM_MODEL,
        api_key=config.LLM_API_KEY,  # type: ignore[arg-type]
        base_url=config.LLM_BASE_URL,
        temperature=config.LLM_TEMPERATURE,
        max_retries=config.LLM_MAX_RETRIES,
        timeout=config.LLM_REQUEST_TIMEOUT,
    )


def trim_history(messages: Sequence[BaseMessage], limit: int | None = None) -> list[BaseMessage]:
    """只保留最近若干条消息。

    截断点会向前对齐到最近的 HumanMessage，这样不会出现「有 ToolMessage 却没有
    对应 tool_call」的非法消息序列（OpenAI 接口会直接报 400）。
    """
    max_messages = config.MAX_HISTORY_MESSAGES if limit is None else limit
    items = list(messages)
    if max_messages <= 0 or len(items) <= max_messages:
        return items

    tail = items[-max_messages:]
    start = next((i for i, msg in enumerate(tail) if isinstance(msg, HumanMessage)), None)
    if start is None:
        # 兜底：至少不要把开头的 ToolMessage 孤儿留在最前面
        start = next((i for i, msg in enumerate(tail) if not isinstance(msg, ToolMessage)), 0)
    return tail[start:]


def should_continue(state: MessagesState) -> NodeName:
    """条件边：LLM 要求调工具就去 tools 节点，否则结束本轮。"""
    last_message = state["messages"][-1]
    if getattr(last_message, "tool_calls", None):
        return "tools"
    return "end"


def create_checkpointer() -> MemorySaver | None:
    """按配置创建会话记忆；AGENT_ENABLE_CHECKPOINTER=0 时返回 None。"""
    return MemorySaver() if config.ENABLE_CHECKPOINTER else None


def build_run_config(
    thread_id: str | None = None, recursion_limit: int | None = None
) -> dict[str, Any]:
    """组装 invoke/stream 的 config：迭代上限 + 会话 thread_id。"""
    run_config: dict[str, Any] = {
        "recursion_limit": config.RECURSION_LIMIT if recursion_limit is None else recursion_limit
    }
    if thread_id:
        run_config["configurable"] = {"thread_id": thread_id}
    return run_config


def build_code_review_agent(
    llm: BaseChatModel | None = None,
    tools: Sequence[BaseTool] | None = None,
    checkpointer: Any = None,
):
    """构建并编译代码审查 Agent。

    参数 llm / tools 可注入，便于测试时替换成假模型，不必真的请求接口。
    """
    model = llm if llm is not None else get_llm()
    tool_list = list(tools) if tools is not None else get_all_tools()
    model_with_tools = model.bind_tools(tool_list)
    tool_node = ToolNode(tool_list)

    def call_model(state: MessagesState) -> dict[str, list[BaseMessage]]:
        history = trim_history(state["messages"])
        messages: list[BaseMessage] = [SystemMessage(content=SYSTEM_PROMPT), *history]
        response = model_with_tools.invoke(messages)
        return {"messages": [response]}

    workflow = StateGraph(MessagesState)
    workflow.add_node("agent", call_model)
    workflow.add_node("tools", tool_node)
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {"tools": "tools", "end": END},
    )
    workflow.add_edge("tools", "agent")
    return workflow.compile(checkpointer=checkpointer)


class _StructureOnlyModel:
    """仅用于导出图结构：不会真正调用模型，因此离线也能画图。"""

    def bind_tools(self, tools: Sequence[BaseTool]) -> "_StructureOnlyModel":
        return self

    def invoke(self, messages: Any) -> AIMessage:  # pragma: no cover - 结构模式不会执行
        raise RuntimeError("结构图模式不会真正调用模型。")


def describe_graph() -> str:
    """导出图结构的 Mermaid 文本（用于文档、调试，不需要 API Key）。"""
    app = build_code_review_agent(llm=_StructureOnlyModel(), checkpointer=None)  # type: ignore[arg-type]
    return app.get_graph().draw_mermaid()
