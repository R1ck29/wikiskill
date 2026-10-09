"""WikiSkill must not talk to the network itself: every model call goes through the CLIs."""
import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "wikiskill"
NETWORK_MODULES = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "requests", "httpx", "aiohttp"}


class NoNetworkTest(unittest.TestCase):
    def test_package_imports_no_network_modules(self):
        for path in PACKAGE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    self.assertNotIn(name.split(".")[0], NETWORK_MODULES, f"{path.name} imports {name}")


if __name__ == "__main__":
    unittest.main()
