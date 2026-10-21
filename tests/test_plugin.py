"""Plugin packaging: the skill launcher, the manifests and tools/integrate.py."""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import TempDirTest
from wikiskill import __version__
from wikiskill.util import write_json

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "wikiskill"
LAUNCHER = SKILL / "scripts" / "run_wikiskill.py"


def run(*argv, cwd=None):
    return subprocess.run([sys.executable, *map(str, argv)], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", timeout=120, check=False)


class LauncherTest(TempDirTest):
    def test_runs_package_from_repo_root(self):
        result = run(LAUNCHER, "--version", cwd=self.root)  # cwd outside the repo: no accidental import
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(__version__, result.stdout)

    def test_passes_exit_codes_through(self):
        result = run(LAUNCHER, "check", "--suite", self.root / "missing", "--no-call", cwd=self.root)
        self.assertEqual(result.returncode, 2)

    def test_prefers_package_inside_skill_folder(self):
        skill = self.root / "skill"
        shutil.copytree(SKILL, skill)
        shutil.copytree(REPO / "wikiskill", skill / "wikiskill", ignore=shutil.ignore_patterns("__pycache__"))
        (skill / "wikiskill" / "__init__.py").write_text('__version__ = "vendored-copy"\n', encoding="utf-8")
        result = run(skill / "scripts" / "run_wikiskill.py", "--version", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("vendored-copy", result.stdout)

    def test_reports_missing_package(self):
        skill = self.root / "skill"
        shutil.copytree(SKILL, skill)
        result = run(skill / "scripts" / "run_wikiskill.py", "--version", cwd=self.root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("wikiskill package was not found", result.stderr)

    def test_picks_the_newest_python3_minor_on_path(self):
        sys.path.insert(0, str(LAUNCHER.parent))
        self.addCleanup(sys.path.remove, str(LAUNCHER.parent))
        import run_wikiskill
        found = {"python3.10": "/x/python3.10", "python3.12": "/x/python3.12"}
        with mock.patch("shutil.which", found.get):
            self.assertEqual(run_wikiskill.newer_python(), "/x/python3.12")
        with mock.patch("shutil.which", lambda name: None):
            self.assertIsNone(run_wikiskill.newer_python())

    def test_frontmatter_and_launcher_reference(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        front = text.split("---")[1]
        self.assertIn("name: wikiskill", front)
        self.assertIn("description:", front)
        self.assertIn('"${CLAUDE_SKILL_DIR}/scripts/run_wikiskill.py"', text)


class IntegrateTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.host = self.root / "harness"
        (self.host / ".claude-plugin").mkdir(parents=True)
        (self.host / ".claude-plugin" / "plugin.json").write_text('{"name": "harness"}', encoding="utf-8")

    def integrate(self, *argv):
        return run(REPO / "tools" / "integrate.py", *argv, cwd=self.root)

    def test_copy_is_self_contained(self):
        result = self.integrate(self.host)
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = self.host / "skills" / "wikiskill"
        for path in ("SKILL.md", "scripts/run_wikiskill.py", "wikiskill/__init__.py",
                     "wikiskill/prompts/skill_proposer.md", "docs/SUITE_FORMAT.md", "examples/sales-analysis/suite.json"):
            self.assertTrue((dest / path).is_file(), path)
        self.assertFalse(list(dest.rglob("__pycache__")))
        moved = self.root / "elsewhere"  # the repository is not needed any more
        shutil.move(str(self.host), str(moved))
        launcher = moved / "skills" / "wikiskill" / "scripts" / "run_wikiskill.py"
        result = run(launcher, "check", "--suite", moved / "skills" / "wikiskill" / "examples" / "sales-analysis",
                     "--no-call", "--backend-config", self.backend_config(), cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def backend_config(self):
        path = self.root / "backend.json"
        write_json(path, {"claude": {"command": Path(sys.executable).name}})
        return path

    def test_refuses_to_overwrite_without_force(self):
        self.assertEqual(self.integrate(self.host).returncode, 0)
        result = self.integrate(self.host)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--force", result.stderr)
        self.assertEqual(self.integrate(self.host, "--force").returncode, 0)

    def test_force_never_deletes_a_foreign_folder(self):
        foreign = self.host / "skills" / "wikiskill"
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("someone else's skill", encoding="utf-8")
        result = self.integrate(self.host, "--force")
        self.assertEqual(result.returncode, 2)
        self.assertEqual((foreign / "SKILL.md").read_text(encoding="utf-8"), "someone else's skill")

    def test_rejects_targets_inside_the_repository(self):
        for target in (REPO / "docs", REPO / "skills", REPO):
            result = self.integrate(target, "--skills-dir")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("inside this repository", result.stderr)
            self.assertFalse((target / "wikiskill" / "docs").exists())

    def test_rejects_a_linked_skills_folder_that_leads_into_the_repository(self):
        try:
            (self.host / "skills").symlink_to(REPO / "skills", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available")
        result = self.integrate(self.host, "--force")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("inside this repository", result.stderr)
        self.assertTrue((REPO / "skills" / "wikiskill" / "SKILL.md").is_file())

    def test_target_must_be_a_plugin_unless_skills_dir(self):
        skills = self.root / "my-skills"
        skills.mkdir()
        result = self.integrate(skills)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--skills-dir", result.stderr)
        self.assertEqual(self.integrate(skills, "--skills-dir").returncode, 0)
        self.assertTrue((skills / "wikiskill" / "wikiskill" / "cli.py").is_file())


if __name__ == "__main__":
    unittest.main()
