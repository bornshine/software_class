"""tools/ast_analyzer.py 的测试。"""

import unittest

from tools.ast_analyzer import ast_analyzer


def run(code: str) -> str:
    return ast_analyzer.invoke({"code": code})


class AstAnalyzerTest(unittest.TestCase):
    def test_reports_syntax_error(self) -> None:
        result = run("def broken(:\n    pass")
        self.assertIn("语法错误", result)

    def test_counts_functions_and_classes(self) -> None:
        code = (
            "class Demo:\n"
            "    def a(self):\n"
            "        return 1\n"
            "    def b(self):\n"
            "        return 2\n"
            "\n"
            "def top():\n"
            "    return 3\n"
        )
        result = run(code)
        self.assertIn("函数 3 个", result)
        self.assertIn("模块级 1 个", result)
        self.assertIn("类方法 2 个", result)
        self.assertIn("类 1 个", result)
        self.assertIn("方法 2 个", result)

    def test_detects_mutable_default_argument(self) -> None:
        result = run("def append_item(item, lst=[]):\n    lst.append(item)\n    return lst\n")
        self.assertIn("可变默认参数", result)
        self.assertIn("[Bug]", result)

    def test_detects_bare_except(self) -> None:
        result = run("try:\n    pass\nexcept:\n    pass\n")
        self.assertIn("裸 except", result)

    def test_detects_silently_swallowed_exception(self) -> None:
        result = run("try:\n    pass\nexcept ValueError:\n    pass\n")
        self.assertIn("静默吞掉异常", result)

    def test_detects_high_complexity(self) -> None:
        branches = "\n".join(f"    if x > {i}:\n        x += 1" for i in range(6))
        result = run(f"def busy(x):\n{branches}\n    return x\n")
        self.assertIn("高复杂度函数", result)
        self.assertIn("busy()", result)

    def test_reports_no_issue_for_clean_code(self) -> None:
        result = run("def add(a, b):\n    return a + b\n")
        self.assertIn("未发现明显静态问题", result)
        self.assertIn("所有函数复杂度合理", result)

    def test_nested_function_complexity_is_not_double_counted(self) -> None:
        inner_branches = "\n".join(f"        if y > {i}:\n            pass" for i in range(5))
        code = (
            "def outer(x):\n"
            "    if x:\n"
            "        pass\n"
            "    def inner(y):\n"
            f"{inner_branches}\n"
            "        return y\n"
            "    return inner\n"
        )
        result = run(code)
        # inner 的 6 点复杂度不应被累加到 outer 上
        self.assertIn("inner(): 复杂度 6", result)
        self.assertNotIn("outer(): 复杂度", result)


if __name__ == "__main__":
    unittest.main()
