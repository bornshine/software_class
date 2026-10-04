"""tools/file_reader.py 的测试：目录白名单、敏感文件拦截、大小与编码边界。"""

import tempfile
import unittest
from pathlib import Path

import config
from tools.file_reader import file_reader


class FileReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        # 保存全局配置，测试结束后还原，避免互相污染
        self._allowed_backup = list(config.ALLOWED_ROOTS)
        self._max_bytes = config.MAX_FILE_BYTES
        self._max_chars = config.MAX_FILE_CHARS
        config.add_allowed_root(self.root)

    def tearDown(self) -> None:
        config.ALLOWED_ROOTS[:] = self._allowed_backup
        config.MAX_FILE_BYTES = self._max_bytes
        config.MAX_FILE_CHARS = self._max_chars
        self._tmp.cleanup()

    def _write(self, name: str, content: str) -> Path:
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_reads_file_inside_allowed_root(self) -> None:
        path = self._write("sample.py", "print('hello')\n")
        result = file_reader.invoke({"file_path": str(path)})
        self.assertIn("📄 文件: sample.py", result)
        self.assertIn("print('hello')", result)
        self.assertIn("总行数: 2", result)

    def test_rejects_missing_file(self) -> None:
        result = file_reader.invoke({"file_path": str(self.root / "nope.py")})
        self.assertIn("文件不存在", result)

    def test_rejects_directory(self) -> None:
        result = file_reader.invoke({"file_path": str(self.root)})
        self.assertIn("不是文件", result)

    def test_rejects_empty_path(self) -> None:
        self.assertIn("未提供文件路径", file_reader.invoke({"file_path": "   "}))

    def test_rejects_sensitive_file_names(self) -> None:
        for name in (".env", ".env.local", "id_rsa", "server.pem", "cert.key"):
            with self.subTest(name=name):
                path = self._write(name, "SECRET=1\n")
                result = file_reader.invoke({"file_path": str(path)})
                self.assertIn("拒绝读取敏感文件", result)
                self.assertNotIn("SECRET=1", result)

    def test_rejects_path_outside_allowed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other).resolve() / "outside.py"
            outside.write_text("print('outside')\n", encoding="utf-8")
            result = file_reader.invoke({"file_path": str(outside)})
        self.assertIn("不在允许的目录内", result)
        self.assertIn("--root", result)

    def test_rejects_non_utf8_file(self) -> None:
        path = self.root / "binary.py"
        path.write_bytes(b"\xff\xfe\x00\x01")
        result = file_reader.invoke({"file_path": str(path)})
        self.assertIn("不是 UTF-8", result)

    def test_rejects_oversized_file(self) -> None:
        config.MAX_FILE_BYTES = 10
        path = self._write("big.py", "print('x' * 100)\n")
        result = file_reader.invoke({"file_path": str(path)})
        self.assertIn("文件过大", result)

    def test_truncates_long_content(self) -> None:
        config.MAX_FILE_CHARS = 10
        path = self._write("long.py", "x = 1\n" * 20)
        result = file_reader.invoke({"file_path": str(path)})
        self.assertIn("已截断", result)

    def test_accepts_relative_path(self) -> None:
        self._write("rel.py", "value = 42\n")
        result = file_reader.invoke({"file_path": str(self.root / "rel.py")})
        self.assertIn("value = 42", result)


if __name__ == "__main__":
    unittest.main()
