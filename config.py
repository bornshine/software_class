"""集中配置。

所有可调参数、路径安全边界与执行限制都在这里定义，避免魔数散落在各个模块。
每一项都可以通过环境变量或 .env 覆盖，参见 .env.example。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# 项目位置
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or None
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
LLM_REQUEST_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "60"))

# 这些值视为「还没填」，CLI 会直接提示用户去配置
PLACEHOLDER_API_KEYS = frozenset({"", "your-api-key-here", "your-api-key", "sk-xxx"})


def api_key_configured() -> bool:
    """判断 LLM_API_KEY 是否已经真正填写。"""
    return LLM_API_KEY.strip() not in PLACEHOLDER_API_KEYS


# ---------------------------------------------------------------------------
# Agent 运行时
# ---------------------------------------------------------------------------
# 单次 invoke 允许的最大图步数，防止工具调用死循环
RECURSION_LIMIT = int(os.getenv("AGENT_RECURSION_LIMIT", "25"))
# 送进模型的历史消息上限（超出后从最近一条用户消息开始截断）
MAX_HISTORY_MESSAGES = int(os.getenv("AGENT_MAX_HISTORY_MESSAGES", "40"))
# 是否启用 LangGraph 内置的会话记忆（MemorySaver）
ENABLE_CHECKPOINTER = os.getenv("AGENT_ENABLE_CHECKPOINTER", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

# ---------------------------------------------------------------------------
# file_reader 的读取边界
# ---------------------------------------------------------------------------
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(1024 * 1024)))
MAX_FILE_CHARS = int(os.getenv("MAX_FILE_CHARS", "20000"))

# 无论是否在允许目录内，这些文件名一律拒绝（防止密钥外泄给远端模型）
DENIED_FILE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".htpasswd",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }
)
DENIED_FILE_SUFFIXES = frozenset(
    {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".ppk", ".kdbx"}
)


def _to_path(value: os.PathLike[str] | str) -> Path:
    return Path(value).expanduser().resolve()


def _default_allowed_roots() -> list[Path]:
    """默认只允许访问当前工作目录；可用 ALLOWED_ROOTS 覆盖（os.pathsep 分隔）。"""
    raw = os.getenv("ALLOWED_ROOTS", "").strip()
    if raw:
        return [_to_path(p) for p in raw.split(os.pathsep) if p.strip()]
    return [Path.cwd().resolve()]


ALLOWED_ROOTS: list[Path] = _default_allowed_roots()


def add_allowed_root(path: os.PathLike[str] | str) -> Path:
    """追加一个允许 file_reader 访问的根目录（CLI 的 --root 参数走这里）。"""
    root = _to_path(path)
    if root not in ALLOWED_ROOTS:
        ALLOWED_ROOTS.append(root)
    return root


def _normalized(path: os.PathLike[str] | str) -> str:
    # normcase 让 Windows 上的大小写差异不造成误判
    return os.path.normcase(os.path.abspath(str(path)))


def is_path_allowed(path: os.PathLike[str] | str) -> bool:
    """路径是否落在任一允许的根目录内（调用方应传入 resolve() 之后的结果）。

    注意：这里比较的是解析后的绝对路径，因此符号链接指向白名单之外时同样会被拒绝。
    """
    target = _normalized(path)
    for root in ALLOWED_ROOTS:
        prefix = _normalized(root).rstrip("\\/")
        if target == prefix or target.startswith(prefix + os.sep):
            return True
    return False


def describe_allowed_roots(roots: Iterable[Path] | None = None) -> str:
    """把允许目录拼成人类可读的一行，用于错误提示。"""
    items = list(roots) if roots is not None else list(ALLOWED_ROOTS)
    return "、".join(str(r) for r in items) if items else "（未配置）"


# ---------------------------------------------------------------------------
# code_executor 的执行限制
# ---------------------------------------------------------------------------
EXEC_TIMEOUT_SECONDS = int(os.getenv("EXEC_TIMEOUT_SECONDS", "10"))
EXEC_MAX_OUTPUT_CHARS = int(os.getenv("EXEC_MAX_OUTPUT_CHARS", "5000"))
EXEC_MAX_CODE_CHARS = int(os.getenv("EXEC_MAX_CODE_CHARS", "20000"))

# 允许 import 的标准库白名单（取顶层包名）
EXEC_ALLOWED_IMPORTS = frozenset(
    {
        "math",
        "cmath",
        "statistics",
        "random",
        "decimal",
        "fractions",
        "json",
        "re",
        "string",
        "textwrap",
        "datetime",
        "collections",
        "itertools",
        "functools",
        "heapq",
        "bisect",
        "copy",
        "enum",
        "dataclasses",
        "typing",
        "uuid",
        "hashlib",
        "base64",
        "unicodedata",
        "pprint",
        "operator",
    }
)

# 明确禁止的内置函数调用
EXEC_DENIED_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "exit",
        "quit",
        "memoryview",
    }
)

# 明确禁止的属性名（模块被禁之后仍可能通过对象间接访问到的危险方法）。
# 这里刻意保持"窄"：list.remove() / json.loads() 这类正常用法不能被误杀，
# 危险模块本身已在 import 白名单里被挡住。
EXEC_DENIED_ATTRS = frozenset(
    {
        "system",
        "popen",
        "spawn",
        "spawnl",
        "spawnv",
        "spawnve",
        "execv",
        "execve",
        "execvp",
        "fork",
        "forkpty",
        "kill",
        "killpg",
        "rmtree",
        "putenv",
        "getenv",
        "environ",
        "import_module",
    }
)
