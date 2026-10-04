#!/usr/bin/env python3
"""代码审查 Agent（LangGraph 版）命令行界面。

用法示例：
    python cli.py                          # 审查当前目录下的文件
    python cli.py --root D:\\code           # 额外允许 file_reader 访问 D:\\code
    python cli.py --graph agent_graph.mmd  # 离线导出图结构（Mermaid）后退出
    python cli.py --no-memory              # 关闭会话记忆，每轮都是无状态问答

交互命令：quit / exit / q 退出，clear 清空当前会话。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

import config
from agent.graph import (
    build_code_review_agent,
    build_run_config,
    create_checkpointer,
    describe_graph,
)

load_dotenv()

console = Console()

BANNER = "🔍 代码审查 Agent（LangGraph 版）"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="基于 LangGraph 的代码审查 Agent",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="DIR",
        help="额外允许 file_reader 访问的目录，可重复指定",
    )
    parser.add_argument(
        "--graph",
        nargs="?",
        const="agent_graph.mmd",
        metavar="PATH",
        help="导出 LangGraph 结构图（Mermaid 文本）后退出，默认 agent_graph.mmd",
    )
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help="关闭会话记忆（不使用 checkpointer）",
    )
    return parser.parse_args(argv)


def _as_text(content: Any) -> str:
    """把消息内容统一转成字符串（兼容 str 与 content block 列表两种形态）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return "" if content is None else str(content)


def _brief(args: Any, limit: int = 80) -> str:
    """把工具参数压缩成一行，便于在终端回显。"""
    try:
        text = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(args)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def run_once(app: Any, user_input: str, thread_id: str) -> str:
    """跑一轮对话：流式打印工具调用轨迹，返回最终回答文本。"""
    run_config = build_run_config(thread_id=thread_id)
    answer = ""
    for chunk in app.stream(
        {"messages": [HumanMessage(content=user_input)]},
        config=run_config,
        stream_mode="updates",
    ):
        for update in chunk.values():
            for message in update.get("messages", []):
                tool_calls = getattr(message, "tool_calls", None)
                if tool_calls:
                    for call in tool_calls:
                        console.print(
                            f"[dim]🔧 调用 {call['name']}({_brief(call.get('args'))})[/dim]"
                        )
                elif isinstance(message, ToolMessage):
                    console.print(
                        f"[dim]↩︎  {getattr(message, 'name', 'tool')} "
                        f"返回 {len(str(message.content))} 字符[/dim]"
                    )
                elif isinstance(message, AIMessage):
                    text = _as_text(message.content)
                    if text.strip():
                        answer = text
    return answer


def export_graph(destination: str) -> None:
    """离线导出图结构并打印。"""
    mermaid = describe_graph()
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write(mermaid + "\n")
    console.print(f"[green]✅ 已导出图结构：{destination}[/green]\n")
    console.print(Panel(mermaid, title="LangGraph 结构（Mermaid）", border_style="blue"))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    for root in args.root:
        try:
            config.add_allowed_root(root)
        except OSError as exc:
            console.print(f"[red]❌ 无效的 --root 目录：{root}（{exc}）[/red]")
            return 2

    if args.graph:
        export_graph(args.graph)
        return 0

    console.print(f"\n[bold blue]{BANNER}[/bold blue]")
    console.print("💡 输入需求，'quit' 退出，'clear' 清空对话\n")

    if not config.api_key_configured():
        console.print(
            Panel.fit(
                "[red]⚠️  未配置 LLM_API_KEY[/red]\n\n"
                "请复制 .env.example 为 .env 并填写：\n"
                "  LLM_API_KEY=your-api-key\n"
                "  LLM_BASE_URL=https://api.deepseek.com/v1\n"
                "  LLM_MODEL=deepseek-chat",
                border_style="red",
            )
        )
        return 1

    console.print(f"[dim]📁 允许访问的目录：{config.describe_allowed_roots()}[/dim]")
    console.print(
        f"[dim]🧠 会话记忆：{'开启（MemorySaver）' if not args.no_memory else '关闭'}[/dim]"
    )
    console.print("[dim]正在初始化 LangGraph Agent...[/dim]")

    checkpointer = None if args.no_memory else create_checkpointer()
    try:
        app = build_code_review_agent(checkpointer=checkpointer)
    except Exception as exc:  # 初始化失败（配置错误等）
        console.print(f"[red]❌ 初始化失败：{type(exc).__name__}: {exc}[/red]")
        return 1
    console.print("[green]✅ Agent 已就绪[/green]\n")

    thread_id = uuid.uuid4().hex[:8]

    while True:
        try:
            user_input = console.input("[bold cyan]你 ›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]👋 再见！[/yellow]")
            return 0

        if not user_input:
            continue

        lowered = user_input.lower()
        if lowered in {"quit", "exit", "q"}:
            console.print("\n[yellow]👋 再见！[/yellow]")
            return 0
        if lowered in {"clear", "/clear"}:
            thread_id = uuid.uuid4().hex[:8]
            console.print("\n对话已清空\n")
            continue

        console.print("\n[cyan]🔍 Agent 思考中...[/cyan]")

        try:
            answer = run_once(app, user_input, thread_id)
        except GraphRecursionError:
            console.print(
                Panel.fit(
                    f"[yellow]⚠️  已达到最大图步数（{config.RECURSION_LIMIT} 步），本轮中止。[/yellow]\n"
                    "可以把问题拆小一些，或调大环境变量 AGENT_RECURSION_LIMIT。",
                    border_style="yellow",
                )
            )
            continue
        except KeyboardInterrupt:
            console.print("\n[yellow]⏹  已中断本轮对话[/yellow]\n")
            continue
        except Exception as exc:
            console.print(f"\n[red]❌ 出错了: {type(exc).__name__}: {exc}[/red]\n")
            continue

        if not answer:
            console.print(
                "[yellow]⚠️  本轮没有生成最终结论（可能只完成了工具调用），请再补充一句。[/yellow]\n"
            )
            continue

        console.print(
            Panel(
                Markdown(answer),
                title="[bold green]审查结果[/bold green]",
                border_style="green",
                padding=(1, 2),
            )
        )


if __name__ == "__main__":
    sys.exit(main())
