"""Generate the sales-analysis example suite (deterministic) and verify its answers.

    python examples/sales-analysis/make_tasks.py           # regenerate files/ and tasks.jsonl
    python examples/sales-analysis/make_tasks.py --verify  # re-check every answer with SQLite

The data has the quirks real exports have: amounts with commas, yen signs and full-width
digits, three date formats, returned and cancelled orders, and double-posted rows.
Answers also follow an unwritten in-house convention that the agent is never told and has
to learn from feedback: money is reported tax-excluded, computed on the aggregate
(total or average of tax-included amounts, then / 1.1, fractions of a yen truncated).
Returns are subtracted, cancellations ignored, duplicate rows counted once.
"""
import argparse
import json
import random
import re
import sqlite3
import unicodedata
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORES = ["東京", "大阪", "名古屋", "福岡"]
PRODUCTS = ["コーヒー豆", "紅茶", "マグカップ", "ドリッパー"]
PRICES = {"コーヒー豆": 1200, "紅茶": 800, "マグカップ": 1500, "ドリッパー": 2400}
SPLITS = ["train"] * 8 + ["val"] * 6 + ["test"] * 10
QUESTION_TYPES = ["net_sales", "completed_orders", "avg_order"]


def fmt_amount(value: int, rng: random.Random) -> str:
    style = rng.randrange(4)
    if style == 0:
        return str(value)
    if style == 1:
        return f"{value:,}"
    if style == 2:
        return f"¥{value:,}"
    return unicodedata.normalize("NFKC", str(value)).translate(str.maketrans("0123456789", "０１２３４５６７８９")) + "円"


def fmt_date(month: int, day: int, rng: random.Random) -> str:
    return [f"2026-{month:02d}-{day:02d}", f"2026/{month}/{day}", f"2026年{month}月{day}日"][rng.randrange(3)]


def make_rows(rng: random.Random):
    rows, clean = [], []
    for n in range(1, 41):
        month, day = rng.choice([1, 2, 3]), rng.randint(1, 28)
        store, product = rng.choice(STORES), rng.choice(PRODUCTS)
        qty = rng.randint(1, 5)
        status = rng.choices(["完了", "返品", "キャンセル"], [8, 1, 1])[0]
        amount = qty * PRICES[product]
        order = f"A{n:04d}"
        row = [order, fmt_date(month, day, rng), store, product, str(qty), fmt_amount(amount, rng), status]
        rows.append(row)
        clean.append({"order": order, "month": month, "store": store, "product": product,
                      "amount": amount, "status": status})
        if rng.random() < 0.12:  # double-posted export row
            rows.append(list(row))
    rng.shuffle(rows)
    return rows, clean


def tax_excluded(numerator: int, denominator: int = 1) -> int:
    """numerator / denominator / 1.1, truncated toward zero, in exact integer arithmetic."""
    value, divisor = numerator * 10, denominator * 11
    return value // divisor if value >= 0 else -((-value) // divisor)


def answer(kind: str, clean, store: str, month: int, product: str):
    sel = [r for r in clean if r["store"] == store and r["month"] == month]
    if kind == "net_sales":
        net = sum(r["amount"] for r in sel if r["status"] == "完了") - sum(r["amount"] for r in sel if r["status"] == "返品")
        return tax_excluded(net)
    if kind == "completed_orders":
        return sum(1 for r in clean if r["product"] == product and r["status"] == "完了")
    done = [r["amount"] for r in sel if r["status"] == "完了"]
    return tax_excluded(sum(done), len(done))


def question(kind: str, store: str, month: int, product: str) -> str:
    if kind == "net_sales":
        return f"sales.csv の {store}店 の 2026年{month}月 の純売上（円）はいくらですか。"
    if kind == "completed_orders":
        return f"sales.csv で商品「{product}」の完了した注文は何件ですか（全店舗・全期間）。"
    return f"sales.csv の {store}店 の 2026年{month}月 の完了注文1件あたりの平均金額（円、整数）はいくらですか。"


def generate() -> None:
    files = HERE / "files"
    tasks = []
    for i, split in enumerate(SPLITS):
        rng = random.Random(1000 + i)
        rows, clean = make_rows(rng)
        kind = QUESTION_TYPES[i % 3]
        store, month, product = rng.choice(STORES), rng.choice([1, 2, 3]), rng.choice(PRODUCTS)
        if kind == "avg_order":  # make sure there is at least one completed order to average
            store, month = next((r["store"], r["month"]) for r in clean if r["status"] == "完了")
        task_id = f"sales-{i + 1:02d}"
        folder = files / task_id
        folder.mkdir(parents=True, exist_ok=True)
        lines = ["注文ID,日付,店舗,商品,数量,金額,ステータス"] + [",".join(f'"{c}"' if "," in c else c for c in r) for r in rows]
        (folder / "sales.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        tasks.append({"id": task_id, "split": split, "prompt": question(kind, store, month, product),
                      "answer": str(answer(kind, clean, store, month, product)),
                      "files": [f"{task_id}/sales.csv", "DATA_README.md"]})
    (HERE / "tasks.jsonl").write_text("\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n",
                                      encoding="utf-8")
    print(f"wrote {len(tasks)} tasks")


# ----- independent verification (SQLite + separate parsing code) ------------------------
def _yen(text: str) -> int:
    return int(re.sub(r"[^0-9]", "", unicodedata.normalize("NFKC", text)))


def _month(text: str) -> int:
    return int(re.split(r"[-/年月日]", text)[1])


def verify() -> None:
    import csv
    db = sqlite3.connect(":memory:")
    bad = 0
    for task in map(json.loads, (HERE / "tasks.jsonl").read_text(encoding="utf-8").splitlines()):
        db.execute("DROP TABLE IF EXISTS s")
        db.execute("CREATE TABLE s (oid TEXT, month INT, store TEXT, product TEXT, amount INT, status TEXT)")
        with open(HERE / "files" / task["files"][0], encoding="utf-8") as f:
            raw = list(csv.DictReader(f))
        unique = {tuple(r.values()): r for r in raw}.values()  # duplicates are identical rows
        db.executemany("INSERT INTO s VALUES (?,?,?,?,?,?)", [
            (r["注文ID"], _month(r["日付"]), r["店舗"], r["商品"], _yen(r["金額"]), r["ステータス"]) for r in unique])
        got = _sql_answer(db, task["prompt"])
        ok = str(got) == task["answer"]
        bad += not ok
        print(f"{task['id']}: expected {task['answer']}, sqlite {got} {'OK' if ok else 'MISMATCH'}")
    raise SystemExit(1 if bad else 0)


def _sql_answer(db, prompt: str):
    m = re.search(r"(\S+)店 の 2026年(\d)月", prompt)
    if "純売上" in prompt:
        q = ("SELECT COALESCE(SUM(CASE status WHEN '完了' THEN amount WHEN '返品' THEN -amount ELSE 0 END),0) "
             "FROM s WHERE store=? AND month=?")
        net = db.execute(q, (m.group(1), int(m.group(2)))).fetchone()[0]
        return int(Fraction(net) / Fraction(11, 10))  # int() truncates toward zero
    if "平均" in prompt:
        q = "SELECT SUM(amount), COUNT(*) FROM s WHERE store=? AND month=? AND status='完了'"
        total, count = db.execute(q, (m.group(1), int(m.group(2)))).fetchone()
        return int(Fraction(total, count) / Fraction(11, 10))
    product = re.search(r"商品「(.+?)」", prompt).group(1)
    return db.execute("SELECT COUNT(*) FROM s WHERE product=? AND status='完了'", (product,)).fetchone()[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    verify() if parser.parse_args().verify else generate()
