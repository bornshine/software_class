"""tools/code_executor.py 的测试：受限执行、错误输出、超时与截断。"""

import unittest

import config
from tools.code_executor import build_child_env, code_executor


def run(code: str) -> str:
    return code_executor.invoke({"code": code})


class CodeExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._timeout = config.EXEC_TIMEOUT_SECONDS
        self._max_output = config.EXEC_MAX_OUTPUT_CHARS

    def tearDown(self) -> None:
        config.EXEC_TIMEOUT_SECONDS = self._timeout
        config.EXEC_MAX_OUTPUT_CHARS = self._max_output

    def test_runs_simple_code(self) -> None:
        result = run("print(1 + 1)")
        self.assertIn("2", result)
        self.assertIn("正常完成", result)

    def test_runs_whitelisted_import(self) -> None:
        result = run("import math\nprint(math.factorial(5))")
        self.assertIn("120", result)
        self.assertIn("正常完成", result)

    def test_rejects_dangerous_code(self) -> None:
        result = run("import os\nos.system('echo hacked')")
        self.assertIn("⛔ 已拒绝执行", result)
        self.assertIn("os", result)

    def test_reports_runtime_error(self) -> None:
        result = run("raise ValueError('boom')")
        self.assertIn("ValueError", result)
        self.assertIn("异常退出", result)

    def test_reports_empty_output(self) -> None:
        result = run("x = 1")
        self.assertIn("没有任何输出", result)

    def test_truncates_long_output(self) -> None:
        config.EXEC_MAX_OUTPUT_CHARS = 20
        result = run("print('x' * 500)")
        self.assertIn("仅保留前 20 个字符", result)

    def test_times_out_and_kills_process(self) -> None:
        config.EXEC_TIMEOUT_SECONDS = 2
        result = run("while True:\n    pass")
        self.assertIn("执行超时", result)

    def test_child_env_does_not_leak_api_key(self) -> None:
        env = build_child_env()
        self.assertNotIn("LLM_API_KEY", env)
        self.assertTrue(all("KEY" not in name for name in env))


if __name__ == "__main__":
    unittest.main()
