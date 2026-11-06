"""The reminder hook (skills/wikiskill/scripts/judgement_hook.py) and its registration by tools/integrate.py."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.fakes import TempDirTest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "skills" / "wikiskill" / "scripts" / "judgement_hook.py"
sys.path.insert(0, str(HOOK.parent))
import judgement_hook  # noqa: E402


def call_hook(command, prompt):
    event = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, ensure_ascii=False)
    return subprocess.run(command, shell=True, input=event.encode("utf-8"), capture_output=True, timeout=60, check=False)


class HookScriptTest(unittest.TestCase):
    def test_judgements_get_the_reminder(self):
        for prompt in ("良い", "違う、正しくは 1234", "それじゃなくて千円単位で", "間違ってる", "完璧！",
                       "Wrong, it should be 1234", "perfect", "これはwrongです", "greatです"):
            self.assertEqual(judgement_hook.note_for(prompt), judgement_hook.NOTE, prompt)

    def test_other_prompts_get_nothing(self):
        for prompt in ("", "売上を集計して", "/wikiskill", "ok, do it", "good" + "x" * 3000, "goodness", "ダメージを集計して"):
            self.assertIsNone(judgement_hook.note_for(prompt), prompt[:20])

    def test_the_script_prints_the_context_or_nothing_and_never_fails(self):
        cmd = f'"{sys.executable}" "{HOOK}"'
        out = call_hook(cmd, "違う、正しくは 1234")
        self.assertEqual(out.returncode, 0)
        context = json.loads(out.stdout)["hookSpecificOutput"]
        self.assertEqual((context["hookEventName"], context["additionalContext"]),
                         ("UserPromptSubmit", judgement_hook.NOTE))
        self.assertEqual(call_hook(cmd, "売上を集計して").stdout, b"")
        broken = subprocess.run(cmd, shell=True, input=b"{not json", capture_output=True, timeout=60, check=False)
        self.assertEqual((broken.returncode, broken.stdout, broken.stderr), (0, b"", b""))


class RegisterTest(TempDirTest):
    def integrate(self, target, *argv):
        return subprocess.run([sys.executable, str(REPO / "tools" / "integrate.py"), str(target), *argv],
                              cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)

    def hook_commands(self, settings):
        data = json.loads(settings.read_text(encoding="utf-8"))
        return [h["command"] for g in data.get("hooks", {}).get("UserPromptSubmit", []) for h in g["hooks"]]

    def test_global_settings_keep_everything_else_and_hold_one_hook(self):
        claude = self.root / "home" / ".claude"
        (claude / "skills").mkdir(parents=True)
        other = {"type": "command", "command": "echo mine"}
        settings = claude / "settings.json"
        settings.write_text(json.dumps({"model": "opus", "hooks": {
            "UserPromptSubmit": [{"hooks": [other]}], "Stop": [{"hooks": [other]}]}}), encoding="utf-8")
        for argv in ((), ("--force",)):  # an update replaces the hook instead of adding a second one
            result = self.integrate(claude / "skills", "--skills-dir", *argv)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hook: UserPromptSubmit added to", result.stdout)
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual((data["model"], data["hooks"]["Stop"]), ("opus", [{"hooks": [other]}]))
        commands = self.hook_commands(settings)
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0], "echo mine")
        self.assertIn(str(claude / "skills" / "wikiskill" / "scripts" / "judgement_hook.py"), commands[1])
        self.assertTrue((claude / "settings.json.wikiskill.bak").is_file())
        out = call_hook(commands[1], "良い")  # the registered command really runs
        self.assertIn("additionalContext", out.stdout.decode())

        result = self.integrate(claude / "skills", "--skills-dir", "--force", "--no-hook")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.hook_commands(settings), ["echo mine"])

    def test_plugin_and_project_targets(self):
        plugin = self.root / "plugin"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "p"}', encoding="utf-8")
        self.assertEqual(self.integrate(plugin).returncode, 0)
        (command,) = self.hook_commands(plugin / "hooks" / "hooks.json")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/skills/wikiskill/scripts/judgement_hook.py", command)
        project = self.root / "project"
        (project / ".claude").mkdir(parents=True)
        self.assertEqual(self.integrate(project).returncode, 0)
        self.assertEqual(len(self.hook_commands(project / ".claude" / "settings.local.json")), 1)
        self.assertFalse((project / ".claude" / "settings.json").exists())  # the shared file is left alone

    def test_a_folder_without_settings_or_a_broken_settings_file(self):
        plain = self.root / "my-skills"
        plain.mkdir()
        result = self.integrate(plain, "--skills-dir")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hook: not added", result.stdout)
        claude = self.root / "home" / ".claude"
        (claude / "skills").mkdir(parents=True)
        (claude / "settings.json").write_text("{broken", encoding="utf-8")
        result = self.integrate(claude / "skills", "--skills-dir")
        self.assertEqual(result.returncode, 1)
        self.assertIn("the skill is installed, but the hook was not changed", result.stderr)
        self.assertTrue((claude / "skills" / "wikiskill" / "SKILL.md").is_file())
        self.assertEqual((claude / "settings.json").read_text(encoding="utf-8"), "{broken")

    def test_a_linked_settings_file_stays_a_link_and_an_unchanged_file_is_not_rewritten(self):
        claude = self.root / "home" / ".claude"
        (claude / "skills").mkdir(parents=True)
        real = self.root / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text('{"model":"opus"}', encoding="utf-8")
        real.chmod(0o600)
        (claude / "settings.json").symlink_to(real)
        self.assertEqual(self.integrate(claude / "skills", "--skills-dir").returncode, 0)
        self.assertTrue((claude / "settings.json").is_symlink())
        self.assertEqual(len(self.hook_commands(real)), 1)
        self.assertEqual(real.stat().st_mode & 0o777, 0o600)
        self.integrate(claude / "skills", "--skills-dir", "--force", "--no-hook")
        hand = '{"model":"opus","hooks":{}}'
        real.write_text(hand, encoding="utf-8")
        self.integrate(claude / "skills", "--skills-dir", "--force", "--no-hook")  # nothing to remove
        self.assertEqual(real.read_text(encoding="utf-8"), hand)

    def test_a_projects_skills_folder_uses_its_personal_settings(self):
        claude = self.root / "project" / ".claude"
        (claude / "skills").mkdir(parents=True)
        self.assertEqual(self.integrate(claude / "skills", "--skills-dir").returncode, 0)
        self.assertEqual(len(self.hook_commands(claude / "settings.local.json")), 1)
        self.assertFalse((claude / "settings.json").exists())

    def test_settings_that_turn_hooks_off_are_reported(self):
        claude = self.root / "home" / ".claude"
        (claude / "skills").mkdir(parents=True)
        (claude / "settings.json").write_text('{"disableAllHooks": true}', encoding="utf-8")
        result = self.integrate(claude / "skills", "--skills-dir")
        self.assertIn("turns hooks off", result.stdout)


if __name__ == "__main__":
    unittest.main()
