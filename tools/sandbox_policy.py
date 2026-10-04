"""代码执行前的静态策略校验（AST 层）。

**这不是安全沙箱边界**，而是一道「策略检查」：
它拦下常见的越权写法，并把明确的中文原因反馈给 LLM，让模型自己改写代码。
真要执行完全不可信的代码，请使用容器 / 虚拟机 / 低权限用户等操作系统级隔离。

相比于「在源码里 search 危险字符串」，AST 校验不会因为注释里出现 os.system 就误杀，
也不会被 __import__("os").system(...) 这类写法轻易绕过。
"""

from __future__ import annotations

import ast

from config import (
    EXEC_ALLOWED_IMPORTS,
    EXEC_DENIED_ATTRS,
    EXEC_DENIED_CALLS,
    EXEC_MAX_CODE_CHARS,
)


def _call_name(node: ast.Call) -> str | None:
    """取被调用者的名字：foo(...) -> foo，a.b(...) -> b。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def check_code_policy(code: str) -> str | None:
    """校验代码是否允许执行。

    返回 None 表示通过；否则返回拒绝原因（中文，可直接回给模型）。
    """
    if not code or not code.strip():
        return "代码为空。"

    if len(code) > EXEC_MAX_CODE_CHARS:
        return f"代码过长（{len(code)} 字符），上限 {EXEC_MAX_CODE_CHARS} 字符。"

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"语法错误：第 {exc.lineno} 行 - {exc.msg}"

    for node in ast.walk(tree):
        reason = _check_node(node)
        if reason:
            return reason
    return None


def _check_node(node: ast.AST) -> str | None:
    # 1) import：只允许白名单内的标准库
    if isinstance(node, ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top not in EXEC_ALLOWED_IMPORTS:
                return f"不允许 import 模块 '{top}'；允许的模块：{_allowed_imports_hint()}"
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            return "不允许相对导入。"
        top = (node.module or "").split(".")[0]
        if top not in EXEC_ALLOWED_IMPORTS:
            return f"不允许 from {node.module} import ...；允许的模块：{_allowed_imports_hint()}"

    # 2) 危险内置函数
    elif isinstance(node, ast.Call):
        name = _call_name(node)
        if name in EXEC_DENIED_CALLS:
            return f"不允许调用 '{name}()'。"
        if name in EXEC_DENIED_ATTRS:
            return f"不允许调用属性方法 '{name}()'。"

    # 3) 双下划线属性访问，阻断 (1).__class__.__bases__ 这类穿透
    elif isinstance(node, ast.Attribute):
        if node.attr.startswith("__"):
            return f"不允许访问双下划线属性 '{node.attr}'。"

    # 4) 双下划线名字，阻断 __import__ / __builtins__ 这类写法
    elif isinstance(node, ast.Name):
        if node.id.startswith("__"):
            return f"不允许使用双下划线名字 '{node.id}'。"

    return None


def _allowed_imports_hint() -> str:
    return ", ".join(sorted(EXEC_ALLOWED_IMPORTS))
