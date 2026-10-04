"""agent/graph.py 的测试：条件边、历史裁剪、图结构与工具调用闭环。

这里用一个脚本化的假模型替换真实 LLM（LangChain 自带的 GenericFakeChatModel
没有实现 bind_tools，无法直接用于本条链路），因此整个测试不需要 API Key，
也不会产生任何网络请求。
"""

import unittest
from typing import Any, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError

import config
from agent.graph import (
    build_code_review_agent,
    build_run_config,
    create_checkpointer,
    describe_graph,
    should_continue,
    trim_history,
)
from tools import ast_analyzer


class ScriptedChatModel:
    """测试替身：按脚本依次返回预设消息，bind_tools 直接返回自身。"""

    def __init__(self, responses: Sequence[BaseMessage]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def bind_tools(self, tools: Any) -> "ScriptedChatModel":
        return self

    def invoke(self, messages: Any, **kwargs: Any) -> BaseMessage:
        self.calls += 1
        if not self._responses:
            raise AssertionError("模型被调用的次数超过了脚本预设")
        return self._responses.pop(0)


class AlwaysToolCallModel(ScriptedChatModel):
    """永远要求调用工具，用来验证 recursion_limit 能兜住死循环。"""

    def __init__(self) -> None:
        super().__init__([])

    def invoke(self, messages: Any, **kwargs: Any) -> BaseMessage:
        self.calls += 1
        return make_tool_call(call_id=f"call_{self.calls}")


def make_tool_call(call_id: str = "call_1", code: str = "x = 1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "ast_analyzer", "args": {"code": code}, "id": call_id, "type": "tool_call"}
        ],
    )


class ShouldContinueTest(unittest.TestCase):
    def test_routes_to_tools_when_tool_calls_present(self) -> None:
        state = {"messages": [HumanMessage(content="hi"), make_tool_call()]}
        self.assertEqual(should_continue(state), "tools")

    def test_routes_to_end_without_tool_calls(self) -> None:
        state = {"messages": [HumanMessage(content="hi"), AIMessage(content="答案")]}
        self.assertEqual(should_continue(state), "end")


class TrimHistoryTest(unittest.TestCase):
    def test_keeps_all_when_under_limit(self) -> None:
        messages = [HumanMessage(content="a"), AIMessage(content="b")]
        self.assertEqual(trim_history(messages, limit=10), messages)

    def test_trims_back_to_human_message(self) -> None:
        messages = [
            HumanMessage(content="h1"),
            AIMessage(content="a1"),
            HumanMessage(content="h2"),
            AIMessage(content="a2"),
            HumanMessage(content="h3"),
            AIMessage(content="a3"),
        ]
        trimmed = trim_history(messages, limit=3)
        self.assertEqual(len(trimmed), 2)
        self.assertIsInstance(trimmed[0], HumanMessage)
        self.assertEqual(trimmed[0].content, "h3")

    def test_never_starts_with_orphan_tool_message(self) -> None:
        messages = [
            HumanMessage(content="h1"),
            make_tool_call(),
            ToolMessage(content="result", tool_call_id="call_1"),
            AIMessage(content="a2"),
        ]
        trimmed = trim_history(messages, limit=2)
        self.assertNotIsInstance(trimmed[0], ToolMessage)

    def test_uses_config_default(self) -> None:
        saved = config.MAX_HISTORY_MESSAGES
        config.MAX_HISTORY_MESSAGES = 2
        try:
            messages = [
                HumanMessage(content="h1"),
                AIMessage(content="a1"),
                AIMessage(content="a2"),
            ]
            self.assertEqual(len(trim_history(messages)), 2)
        finally:
            config.MAX_HISTORY_MESSAGES = saved


class CheckpointerTest(unittest.TestCase):
    def test_returns_memory_saver_by_default(self) -> None:
        saved = config.ENABLE_CHECKPOINTER
        config.ENABLE_CHECKPOINTER = True
        try:
            self.assertIsInstance(create_checkpointer(), MemorySaver)
        finally:
            config.ENABLE_CHECKPOINTER = saved

    def test_returns_none_when_disabled(self) -> None:
        saved = config.ENABLE_CHECKPOINTER
        config.ENABLE_CHECKPOINTER = False
        try:
            self.assertIsNone(create_checkpointer())
        finally:
            config.ENABLE_CHECKPOINTER = saved


class BuildRunConfigTest(unittest.TestCase):
    def test_includes_thread_id_and_recursion_limit(self) -> None:
        run_config = build_run_config(thread_id="t1")
        self.assertEqual(run_config["configurable"]["thread_id"], "t1")
        self.assertEqual(run_config["recursion_limit"], config.RECURSION_LIMIT)

    def test_omits_configurable_without_thread_id(self) -> None:
        self.assertNotIn("configurable", build_run_config())


class GraphStructureTest(unittest.TestCase):
    def test_graph_has_expected_nodes(self) -> None:
        app = build_code_review_agent(
            llm=ScriptedChatModel([AIMessage(content="ok")]),
            tools=[ast_analyzer],
            checkpointer=None,
        )
        nodes = set(app.get_graph().nodes)
        self.assertIn("agent", nodes)
        self.assertIn("tools", nodes)

    def test_describe_graph_works_offline(self) -> None:
        mermaid = describe_graph()
        self.assertIn("agent", mermaid)
        self.assertIn("tools", mermaid)


class EndToEndLoopTest(unittest.TestCase):
    def test_runs_tool_node_then_answers(self) -> None:
        model = ScriptedChatModel([make_tool_call(), AIMessage(content="审查完成")])
        app = build_code_review_agent(llm=model, tools=[ast_analyzer], checkpointer=None)

        result = app.invoke(
            {"messages": [HumanMessage(content="帮我分析 x = 1")]},
            config=build_run_config(),
        )

        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("代码结构", str(tool_messages[0].content))
        self.assertEqual(result["messages"][-1].content, "审查完成")
        self.assertEqual(model.calls, 2)

    def test_recursion_limit_stops_infinite_tool_loop(self) -> None:
        app = build_code_review_agent(
            llm=AlwaysToolCallModel(), tools=[ast_analyzer], checkpointer=None
        )
        with self.assertRaises(GraphRecursionError):
            app.invoke(
                {"messages": [HumanMessage(content="循环")]},
                config=build_run_config(recursion_limit=4),
            )

    def test_checkpointer_keeps_history_across_turns(self) -> None:
        model = ScriptedChatModel([AIMessage(content="第一次回答"), AIMessage(content="第二次回答")])
        app = build_code_review_agent(
            llm=model, tools=[ast_analyzer], checkpointer=MemorySaver()
        )
        run_config = build_run_config(thread_id="thread-1")

        app.invoke({"messages": [HumanMessage(content="第一问")]}, config=run_config)
        result = app.invoke({"messages": [HumanMessage(content="第二问")]}, config=run_config)

        human_messages = [m for m in result["messages"] if isinstance(m, HumanMessage)]
        self.assertEqual(len(human_messages), 2)
        self.assertEqual(result["messages"][-1].content, "第二次回答")


if __name__ == "__main__":
    unittest.main()
