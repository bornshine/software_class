"""在受限环境中执行 Python 代码片段。

与旧版「字符串黑名单 + 在项目目录里直接跑」相比，这里做了四件事：
1. 用 AST 策略校验代码（见 tools/sandbox_policy.py），而不是 search 危险字符串；
2. 每次执行都在全新的临时目录里，被执行的代码看不到项目文件；
3. 子进程环境变量经过裁剪，不会把 .env 里的 API Key 泄露出去；
4. 超时后杀掉整棵进程树（Windows 用 taskkill /T，POSIX 用进程组），不留残留进程。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile

from langchain_core.tools import tool

import config
from tools.sandbox_policy import check_code_policy

# 子进程需要保留的系统变量；其余（尤其是各种 *_API_KEY）一律不传
_ENV_KEEP = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "LANG",
    "LC_ALL",
)


def build_child_env() -> dict[str, str]:
    """构造最小化的子进程环境变量。"""
    env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def kill_process_tree(proc: subprocess.Popen) -> None:
    """超时后终止子进程及其后代进程。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - 兜底分支
        proc.kill()


def _truncate(text: str) -> str:
    limit = config.EXEC_MAX_OUTPUT_CHARS
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（输出过长，仅保留前 {limit} 个字符）"


@tool
def code_executor(code: str) -> str:
    """在受限的临时目录中执行一小段 Python 代码并返回输出。

    当需要验证代码是否有 Bug、确认运行结果时使用。
    只接受受限的 Python 子集：不允许 import os/subprocess/sys 等模块，
    不允许 open/eval/exec/getattr 等调用；被拒绝时请改写代码，而不是绕过限制。
    """
    reason = check_code_policy(code)
    if reason:
        return f"⛔ 已拒绝执行：{reason}"

    workdir = tempfile.mkdtemp(prefix="cr_agent_exec_")
    script_path = os.path.join(workdir, "snippet.py")

    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(code)

        popen_kwargs: dict[str, object] = {
            "cwd": workdir,
            "env": build_child_env(),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        # -I 隔离模式：忽略 PYTHONPATH / 用户 site-packages / 环境变量注入
        proc = subprocess.Popen(  # type: ignore[call-overload]
            [sys.executable, "-I", script_path], **popen_kwargs
        )

        try:
            stdout, stderr = proc.communicate(timeout=config.EXEC_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            return f"⏰ 执行超时（{config.EXEC_TIMEOUT_SECONDS} 秒），进程已终止。"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    parts: list[str] = []
    if stdout:
        parts.append(f"📤 标准输出:\n{_truncate(stdout)}")
    if stderr:
        parts.append(f"❌ 错误输出:\n{_truncate(stderr)}")
    if not parts:
        parts.append("（没有任何输出）")

    ok = proc.returncode == 0
    status = "正常完成" if ok else f"异常退出(码:{proc.returncode})"
    parts.append(f"\n{'✅' if ok else '⚠️'} {status}")
    return "\n".join(parts)
