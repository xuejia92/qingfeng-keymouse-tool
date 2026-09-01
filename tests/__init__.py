"""单元测试包。

为什么单独建 tests/：项目此前一行测试都没有，而 config 的兼容迁移、版本比对、
热键解析这几处全是纯逻辑 + 边界条件，改坏了只能等用户在运行时踩到才发现。

运行（项目根目录）：
    python -m unittest discover -s tests -v
只跑某一个：
    python -m unittest tests.test_config -v
"""