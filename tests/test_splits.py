import json
import unittest
from collections import Counter
from unittest import mock

from tests.fakes import TempDirTest
from tests.test_evolve import write_suite
from wikiskill.config import ConfigError, load_suite
from wikiskill.splits import TEST_FROM, assign, fill_missing
from wikiskill.util import read_text


def rows(n, verdict=None, prefix="r"):
    return [{"id": f"{prefix}{i}", **({"verdict": verdict} if verdict else {})} for i in range(n)]


class AssignTest(unittest.TestCase):
    def test_train_and_val_share_2_to_1_without_test_below_the_threshold(self):
        for n in (2, 3, 6, 12, 30, TEST_FROM - 1):
            got = Counter(assign([], rows(n)))
            self.assertEqual(got["test"], 0, n)
            self.assertEqual(got["val"], round(n / 3), n)
            self.assertGreaterEqual(got["train"], 1)

    def test_six_rows_always_give_four_train_and_two_val(self):
        for bad in range(7):
            new = rows(6 - bad, "good", "g") + rows(bad, "bad", "b")
            self.assertEqual(Counter(assign([], new)), {"train": 4, "val": 2}, bad)

    def test_good_and_bad_results_each_reach_val(self):
        new = rows(9, "good", "g") + rows(6, "bad", "b")
        got = Counter((r["verdict"], s) for r, s in zip(new, assign([], new)))
        self.assertEqual((got["good", "val"], got["bad", "val"]), (3, 2))

    def test_one_task_in_five_goes_to_test_after_the_threshold(self):
        existing = [{"id": f"e{i}", "split": "val" if i % 3 == 2 else "train"} for i in range(TEST_FROM - 1)]
        got = Counter(assign(existing, rows(10)))
        self.assertEqual(got["test"], 2)  # totals 40 and 45
        self.assertEqual(Counter(assign([], rows(60)))["test"], 5)  # 40, 45, 50, 55, 60

    def test_a_lone_split_is_filled_so_a_round_can_start(self):
        self.assertEqual(sorted(assign([], rows(1, "good") + rows(1, "bad", "b"))), ["train", "val"])
        existing = [{"id": "e", "split": "train"}]
        self.assertEqual(assign(existing, rows(1, "good")), ["val"])
        self.assertEqual(assign([{"id": "e", "split": "val"}], rows(1)), ["train"])

    def test_the_result_does_not_depend_on_the_written_order(self):
        new = rows(9)
        forward = dict(zip((r["id"] for r in new), assign([], new)))
        backward = dict(zip((r["id"] for r in new[::-1]), assign([], new[::-1])))
        self.assertEqual(forward, backward)


class FillMissingTest(TempDirTest):
    def test_tasks_without_a_split_get_one_when_the_suite_is_loaded_and_keep_it(self):
        suite = write_suite(self.root / "s")
        lines = [json.dumps({"id": f"n{i}", "prompt": f"q{i}", "answer": "TOTAL"}) for i in range(6)]
        given = json.dumps({"id": "fixed", "split": "test", "prompt": "q", "answer": "TOTAL"})
        (suite / "tasks.jsonl").write_text("\n".join([given, *lines]) + "\n", encoding="utf-8")
        loaded = load_suite(suite)
        self.assertEqual(Counter(t.split for t in loaded.tasks), {"train": 4, "val": 2, "test": 1})
        self.assertEqual(loaded.split("test")[0].id, "fixed")  # a split written by hand is kept
        text = read_text(suite / "tasks.jsonl")
        self.assertTrue((suite / "tasks.jsonl.bak").is_file())
        self.assertEqual(fill_missing(suite / "tasks.jsonl", suite), 0)
        self.assertEqual(read_text(suite / "tasks.jsonl"), text)  # stable: nothing moves later

    def test_new_tasks_added_later_keep_the_old_ones_in_place(self):
        suite = write_suite(self.root / "s")
        before = {t.id: t.split for t in load_suite(suite).tasks}
        with open(suite / "tasks.jsonl", "a", encoding="utf-8") as f:
            f.writelines(json.dumps({"id": f"new{i}", "prompt": "q", "answer": "TOTAL"}) + "\n" for i in range(3))
        after = {t.id: t.split for t in load_suite(suite).tasks}
        self.assertEqual({k: after[k] for k in before}, before)
        self.assertEqual(Counter(after[f"new{i}"] for i in range(3)), {"train": 2, "val": 1})

    def test_an_empty_or_null_split_counts_as_missing(self):
        path = self.root / "tasks.jsonl"
        path.write_text('{"id": "a", "split": null}\n{"id": "b", "split": ""}\n', encoding="utf-8")
        self.assertEqual(fill_missing(path, self.root), 2)
        self.assertEqual(sorted(json.loads(x)["split"] for x in read_text(path).splitlines()), ["train", "val"])

    def test_a_folder_that_cannot_be_written_gives_a_clear_error(self):
        suite = write_suite(self.root / "s")
        (suite / "tasks.jsonl").write_text('{"id": "a", "prompt": "q", "answer": "TOTAL"}\n', encoding="utf-8")
        with mock.patch("wikiskill.splits.write_text", side_effect=PermissionError("read-only")), \
                self.assertRaisesRegex(ConfigError, "write 'split' on every task"):
            load_suite(suite)

    def test_a_broken_line_is_left_for_the_loader(self):
        path = self.root / "tasks.jsonl"
        path.write_text('{"id": "a", "prompt": "q"}\n{broken\n', encoding="utf-8")
        self.assertEqual(fill_missing(path, self.root), 0)
        self.assertFalse((self.root / "tasks.jsonl.bak").exists())


if __name__ == "__main__":
    unittest.main()
