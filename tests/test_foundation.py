import json
import tempfile
import unittest
from pathlib import Path

from wikiskill.config import ConfigError, load_suite
from wikiskill.patching import PatchError, apply_edits, unified_diff
from wikiskill.skills import SkillError, apply_proposal, load_skills, render_skill_section, validate_skill_md
from wikiskill.util import extract_json_object, truncate
from wikiskill.workspace import Workspace

SKILL = "---\nname: csv-cleaning\ndescription: Clean CSV numbers before summing.\n---\n# Steps\n1. Strip commas.\n"


class UtilTest(unittest.TestCase):
    def test_extract_json_prefers_fenced_block(self):
        text = 'thinking {not json}\n```json\n{"a": 1, "b": "{x}"}\n```\n'
        self.assertEqual(extract_json_object(text), {"a": 1, "b": "{x}"})

    def test_extract_json_raw_scan(self):
        self.assertEqual(extract_json_object('Answer: {"op": "x"} trailing'), {"op": "x"})

    def test_extract_json_missing(self):
        with self.assertRaises(ValueError):
            extract_json_object("no json here")

    def test_truncate_keeps_head_and_tail(self):
        text = "H" * 100 + "M" * 1000 + "TAIL"
        out = truncate(text, 300)
        self.assertLessEqual(len(out), 300)
        self.assertTrue(out.startswith("H"))
        self.assertTrue(out.endswith("TAIL"))
        self.assertEqual(truncate("short", 300), "short")


class PatchingTest(unittest.TestCase):
    def test_ops(self):
        text = "a\nb\n"
        text = apply_edits(text, [{"op": "append", "content": "c"}])
        self.assertEqual(text, "a\nb\nc\n")
        text = apply_edits(text, [{"op": "replace", "target": "b\n", "content": "B\n"}])
        text = apply_edits(text, [{"op": "insert_after", "target": "a\n", "content": "a2\n"}])
        self.assertEqual(text, "a\na2\nB\nc\n")

    def test_missing_and_ambiguous_target(self):
        with self.assertRaises(PatchError):
            apply_edits("abc", [{"op": "replace", "target": "zzz", "content": "y"}])
        with self.assertRaises(PatchError):
            apply_edits("x x", [{"op": "replace", "target": "x", "content": "y"}])
        with self.assertRaises(PatchError):
            apply_edits("x", [{"op": "delete", "content": ""}])
        with self.assertRaises(PatchError):
            apply_edits("x", [])
        with self.assertRaises(PatchError):  # overlapping matches are ambiguous too
            apply_edits("aaa", [{"op": "replace", "target": "aa", "content": "X"}])

    def test_diff_new_file(self):
        diff = unified_diff("", "hello\n", "s/SKILL.md", is_new=True)
        self.assertIn("--- /dev/null", diff)
        self.assertIn("+hello", diff)


class SkillsTest(unittest.TestCase):
    def test_validate(self):
        validate_skill_md("csv-cleaning", SKILL)
        for bad_name in ("CSV", "csv_cleaning", "-a", "a--b", "a" * 65, "con", "aux"):
            with self.assertRaises(SkillError):
                validate_skill_md(bad_name, SKILL.replace("csv-cleaning", bad_name))
        with self.assertRaises(SkillError):
            validate_skill_md("other", SKILL)  # name must match directory
        with self.assertRaises(SkillError):
            validate_skill_md("csv-cleaning", "# no frontmatter")

    def test_frontmatter_edge_cases(self):
        validate_skill_md("csv-cleaning", "\ufeff" + SKILL)  # BOM from Windows editors
        with self.assertRaises(SkillError):
            validate_skill_md("abc\n", SKILL.replace("csv-cleaning", "abc"))
        from wikiskill.skills import parse_frontmatter
        meta = parse_frontmatter("---\nname: x\ndescription: \"Handles 'x'\"\n---\n")
        self.assertEqual(meta["description"], "Handles 'x'")

    def test_folded_description(self):
        text = "---\nname: x\ndescription: >\n  multi\n  line\n---\nbody"
        validate_skill_md("x", text)

    def test_apply_create_then_patch_leaves_source_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cand1 = base / "cand1"
            name, diff = apply_proposal(base / "empty", {
                "action": "create", "name": "csv-cleaning", "skill_md": SKILL, "purpose_md": "# Purpose\n"}, cand1)
            self.assertEqual(name, "csv-cleaning")
            self.assertIn("/dev/null", diff)
            cand2 = base / "cand2"
            _, diff2 = apply_proposal(cand1, {"action": "patch", "name": "csv-cleaning", "edits": [
                {"op": "append", "content": "2. Parse dates."}]}, cand2)
            self.assertIn("+2. Parse dates.", diff2)
            self.assertNotIn("Parse dates", (cand1 / "csv-cleaning" / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual([n for n, _ in load_skills(cand2)], ["csv-cleaning"])
            section = render_skill_section(load_skills(cand2))
            self.assertIn('<skill name="csv-cleaning">', section)
            self.assertEqual(render_skill_section([]), "")

    def test_invalid_proposals(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cases = [
                {"action": "delete", "name": "x"},
                {"action": "patch", "name": "missing", "edits": [{"op": "append", "content": "x"}]},
                {"action": "create", "name": "Bad_Name", "skill_md": SKILL, "purpose_md": ""},
                {"action": "create", "name": "csv-cleaning", "skill_md": SKILL},
                {"action": "patch", "name": "../escape", "edits": [{"op": "append", "content": "x"}]},
                "not a dict",
            ]
            for proposal in cases:
                with self.assertRaises(SkillError, msg=str(proposal)):
                    apply_proposal(base / "none", proposal, base / "out")


def write_suite(root: Path, tasks, **overrides):
    (root / "files").mkdir(parents=True, exist_ok=True)
    (root / "files" / "a.csv").write_text("x\n1\n", encoding="utf-8")
    cfg = {
        "name": "demo", "task_description": "demo tasks", "tasks": "tasks.jsonl",
        "system_prompt": "Solve.\n{skill_section}\nAnswer in <answer></answer>.",
        "scorer": {"type": "exact"},
    }
    cfg.update(overrides)
    (root / "suite.json").write_text(json.dumps(cfg), encoding="utf-8")
    (root / "tasks.jsonl").write_text("\n".join(json.dumps(t, ensure_ascii=False) for t in tasks), encoding="utf-8")


class ConfigTest(unittest.TestCase):
    def test_load_valid_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_suite(root, [
                {"id": "t1", "split": "train", "prompt": "日本語の質問", "answer": "1", "files": ["a.csv"]},
                {"id": "v1", "split": "val", "prompt": "q", "answer": "2"},
            ])
            suite = load_suite(root)
            self.assertEqual([t.id for t in suite.split("train")], ["t1"])
            self.assertEqual(suite.split("train")[0].prompt, "日本語の質問")
            self.assertFalse(suite.human_review)
            self.assertEqual(suite.pass_threshold, 1.0)

    def test_errors(self):
        bad_tasks = [
            [{"id": "bad id", "split": "train", "prompt": "q", "answer": "1"}],
            [{"id": "t", "split": "dev", "prompt": "q", "answer": "1"}],
            [{"id": "t", "split": "train", "prompt": "", "answer": "1"}],
            [{"id": "t", "split": "train", "prompt": "q"}],  # exact scorer needs an answer
            [{"id": "t", "split": "train", "prompt": "q", "answer": "1", "files": ["../suite.json"]}],
            [{"id": "t", "split": "train", "prompt": "q", "answer": "1"}] * 2,
            [{"id": "A", "split": "train", "prompt": "q", "answer": "1"},
             {"id": "a", "split": "train", "prompt": "q", "answer": "1"}],  # case-insensitive clash
            [{"id": "abc\n", "split": "train", "prompt": "q", "answer": "1"}],
            [{"id": "CON", "split": "train", "prompt": "q", "answer": "1"}],
            [{"id": "nul.txt", "split": "train", "prompt": "q", "answer": "1"}],
            [{"id": "t", "split": "train", "prompt": "q", "answer": "1", "files": ["../files/a.csv"]}],
        ]
        for tasks in bad_tasks:
            with tempfile.TemporaryDirectory() as tmp:
                write_suite(Path(tmp), tasks)
                with self.assertRaises(ConfigError, msg=str(tasks)):
                    load_suite(Path(tmp))

    def test_answers_optional_for_human_review_and_review_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_suite(root, [{"id": "t", "split": "train", "prompt": "q"}])
            self.assertTrue(load_suite(root, human_review=True).human_review)
            self.assertIsNone(load_suite(root, require_answers=False).tasks[0].answer)
            with self.assertRaises(ConfigError):
                load_suite(root)

    def test_bad_settings_raise_config_error(self):
        task = [{"id": "t", "split": "train", "prompt": "q", "answer": "1"}]
        for overrides in ({"pass_threshold": "high"}, {"human_review": "false"},
                          {"system_prompt_file": "missing.md"}, {"system_prompt_file": 3}):
            with tempfile.TemporaryDirectory() as tmp:
                write_suite(Path(tmp), task, **overrides)
                with self.assertRaises(ConfigError, msg=str(overrides)):
                    load_suite(Path(tmp))

    def test_invalid_regex_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_suite(Path(tmp), [{"id": "t", "split": "train", "prompt": "q", "answer": "(["}],
                        scorer={"type": "regex"})
            with self.assertRaises(ConfigError):
                load_suite(Path(tmp))

    def test_system_prompt_needs_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_suite(Path(tmp), [{"id": "t", "split": "train", "prompt": "q", "answer": "1"}],
                        system_prompt="no placeholder")
            with self.assertRaises(ConfigError):
                load_suite(Path(tmp))


class WorkspaceTest(unittest.TestCase):
    def test_init_and_raw_is_write_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(Path(tmp) / "ws")
            ws.init()
            for name in ("index.md", "logs.md", "skill-impact.md"):
                self.assertTrue((ws.wiki / name).is_file())
            path = ws.write_raw(1, {"t1": "trace"}, {"n": 1})
            self.assertEqual((path / "traces" / "t1.md").read_text(encoding="utf-8"), "trace")
            with self.assertRaises(FileExistsError):
                ws.write_raw(1, {"t1": "other"}, {})
            with self.assertRaises(ValueError):
                ws.write_raw(2, {"A": "upper", "a": "lower"}, {})
            with ws.lock():
                with self.assertRaises(RuntimeError):  # a second run on the same workspace is refused
                    with ws.lock():
                        pass
            self.assertFalse((ws.root / "evolve.lock").exists())
            ws.save_state({"iteration": 1})
            self.assertEqual(ws.load_state(), {"iteration": 1})


if __name__ == "__main__":
    unittest.main()
