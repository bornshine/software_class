"""tools/sandbox_policy.py 的测试：代码执行策略校验。"""

import unittest

from tools.sandbox_policy import check_code_policy


class CheckCodePolicyTest(unittest.TestCase):
    def test_allows_simple_code(self) -> None:
        self.assertIsNone(check_code_policy("print(1 + 1)"))

    def test_allows_whitelisted_imports(self) -> None:
        self.assertIsNone(check_code_policy("import math\nprint(math.sqrt(16))"))
        self.assertIsNone(check_code_policy("import json\nprint(json.dumps({'a': 1}))"))

    def test_rejects_blacklisted_imports(self) -> None:
        for code in ("import os", "import subprocess", "import sys", "import shutil"):
            with self.subTest(code=code):
                reason = check_code_policy(code)
                self.assertIsNotNone(reason)
                self.assertIn("import", reason)

    def test_rejects_from_import(self) -> None:
        reason = check_code_policy("from os import system")
        self.assertIsNotNone(reason)
        self.assertIn("os", reason)

    def test_rejects_relative_import(self) -> None:
        reason = check_code_policy("from . import secret")
        self.assertIsNotNone(reason)
        self.assertIn("相对导入", reason)

    def test_rejects_dynamic_import(self) -> None:
        reason = check_code_policy('__import__("os")')
        self.assertIsNotNone(reason)
        self.assertIn("__import__", reason)

    def test_rejects_dynamic_import_with_attribute_call(self) -> None:
        reason = check_code_policy('__import__("os").system("dir")')
        self.assertIsNotNone(reason)

    def test_rejects_dangerous_builtins(self) -> None:
        for code in ("eval('1+1')", "exec('a=1')", "open('x.txt', 'w')", "getattr(object, 'x')"):
            with self.subTest(code=code):
                self.assertIsNotNone(check_code_policy(code))

    def test_rejects_dunder_attribute_escape(self) -> None:
        reason = check_code_policy("print((1).__class__.__bases__)")
        self.assertIsNotNone(reason)
        self.assertIn("双下划线", reason)

    def test_rejects_empty_code(self) -> None:
        self.assertIsNotNone(check_code_policy("   \n  "))

    def test_rejects_syntax_error(self) -> None:
        reason = check_code_policy("def broken(:\n    pass")
        self.assertIsNotNone(reason)
        self.assertIn("语法错误", reason)

    def test_rejects_oversized_code(self) -> None:
        self.assertIsNotNone(check_code_policy("a = 1\n" * 6000))

    def test_comment_mentioning_danger_is_allowed(self) -> None:
        """回归测试：旧版字符串黑名单会因为注释里出现 os.system 就误杀。"""
        self.assertIsNone(check_code_policy("# os.system('rm -rf /')\nprint('ok')"))

    def test_normal_attribute_calls_are_allowed(self) -> None:
        """回归测试：撤销对 list.remove / json.loads 这类正常用法的误杀。"""
        self.assertIsNone(check_code_policy("xs = [1, 2]\nxs.remove(1)\nprint(xs)"))
        self.assertIsNone(check_code_policy("import json\nprint(json.loads('{\"a\": 1}'))"))


if __name__ == "__main__":
    unittest.main()
