"""演示用的样例代码：故意包含若干典型问题，供代码审查 Agent 分析。

可以直接这样试：
    python cli.py
    你 › 帮我审查 examples/sample_buggy_code.py
"""

import sqlite3  # noqa: F401  (未使用的导入，也是一种可审查的问题)

PASSWORD = "admin123"  # 硬编码口令
DB_PATH = "/var/data/app.db"  # 硬编码绝对路径


def get_user(conn, name):
    """用字符串拼接构造 SQL，存在注入风险。"""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE name = '" + name + "'")
    return cursor.fetchall()


def append_log(entry, log=[]):
    """可变默认参数：多次调用会共享同一个列表。"""
    log.append(entry)
    return log


def average(values):
    """循环多跑一次，越界访问。"""
    total = 0
    for i in range(len(values) + 1):
        total += values[i]
    return total / len(values)


def read_config(path):
    """裸 except 会吞掉所有异常，包括 KeyboardInterrupt。"""
    try:
        with open(path) as handle:
            return handle.read()
    except:
        return None


def check_password(user_input):
    """可以简化为直接返回比较结果。"""
    if user_input == PASSWORD:
        return True
    else:
        return False
