#!/usr/bin/env python3
"""Pre-flight checks on a real engine before trusting systemone_local.py's
answers. Reuses systemone_local.py's prompt template, engine call and token
merging, and reads the same environment variables (ENGINE_URL, ENGINE_MODEL,
TOP_LOGPROBS, MIN_LABEL_MASS, ENGINE_EXTRA_BODY, ENGINE_TIMEOUT_S), so it
measures what the endpoint itself would read.

Part 1 (pass/fail): the engine returns several candidate tokens for the
first answer token, not only the sampled one. With a single candidate every
answer would be a false p=1, which contract_test.py cannot detect.

Part 2 (measurement): ten questions whose STATE does not contain the answer,
each with ten options A-J, so "A" and "I" are both option letters. Prints the
mass on option letters and on "A" and "I" per question, then asks the same
questions with eight options A-H, where "I" is not an option: mass on "I"
there comes from the model starting a sentence ("I cannot ..."), and in a
question with nine or more options that mass would be read as option I.

Checking that each option letter is a single token needs the model's own
tokenizer and is not done here.

Exit status: 0 if part 1 passes and part 2 completes or is skipped; 1
otherwise (engine errors and bad configuration included).
"""
import sys

# Importing systemone_local must not leave a __pycache__ next to the scripts.
sys.dont_write_bytecode = True

from string import ascii_uppercase  # noqa: E402

import systemone_local as s1  # noqa: E402

CHECK = ("水在一大氣壓下，攝氏 100 度會沸騰。", "水在一大氣壓下的沸點是攝氏幾度？", ["0", "50", "100", "200"])

# (STATE, QUESTION, ten options); the STATE never contains the answer.
UNANSWERABLE = [
    ("今天台北晴時多雲，午後有局部陣雨。", "文中提到的訂單編號是哪一個？", [f"#10{i:02d}" for i in range(1, 11)]),
    ("會議改到週四下午三點，地點不變。", "文中提到的產品售價是多少？", [f"NT${i}00" for i in range(1, 11)]),
    ("新版 App 修正了登入後閃退的問題。", "這個問題是誰回報的？", list("甲乙丙丁戊己庚辛壬癸")),
    ("倉庫週末不出貨。", "文中提到的物流單號是哪一個？", [f"TW{i:04d}" for i in range(1, 11)]),
    ("The quarterly report is due next Friday.", "Which city is the customer located in?",
     ["Taipei", "Tokyo", "Seoul", "Singapore", "Sydney", "London", "Paris", "Berlin", "Toronto", "Chicago"]),
    ("請在月底前更新密碼。", "文中提到的會議室是哪一間？", [f"會議室 {i}" for i in range(1, 11)]),
    ("這批貨的包裝改用紙箱。", "客戶的統一編號是哪一個？", [str(12345670 + i) for i in range(10)]),
    ("系統將於凌晨兩點維護。", "文中提到的折扣是多少？", [f"{i * 5}%" for i in range(1, 11)]),
    ("The new logo uses a darker blue.", "What is the invoice total?", [f"${i}00" for i in range(1, 11)]),
    ("午餐改到下午一點。", "文中提到的航班編號是哪一個？", [f"CI {100 + i}" for i in range(1, 11)]),
]


def read_letters(state, question, texts):
    """One engine call with the endpoint's own prompt layout (as in
    systemone_local.readout_options); returns (raw top_logprobs, merged
    masses, option letters)."""
    options = [(f"c{i}", text) for i, text in enumerate(texts)]
    letters = ascii_uppercase[: len(options)]
    lines = "\n".join(f"{letter}) {label}: {desc}" for letter, (label, desc) in zip(letters, options))
    prompt = s1.PROMPT_TEMPLATE.format(state=state, instructions=question, options=lines)
    top, _, _ = s1.call_engine(prompt)
    return top, s1.merge_tokens(top), letters


def top_tokens(merged, n=3):
    ranked = sorted(merged.items(), key=lambda kv: -kv[1])[:n]
    return "  ".join(f"{token!r} {p:.2f}" for token, p in ranked)


def mean(values):
    return sum(values) / len(values) if values else 0.0


def main():
    errors = s1.startup_errors()
    if errors:
        for err in errors:
            print(f"設定錯誤：{err}")
        return 1
    print(f"引擎：{s1.ENGINE_URL}（ENGINE_MODEL={s1.ENGINE_MODEL}，TOP_LOGPROBS={s1.TOP_LOGPROBS}）")

    try:
        top, merged, _ = read_letters(*CHECK)
    except Exception as exc:  # the endpoint would answer 502 for the same failure
        print(f"[1] FAIL：引擎呼叫失敗（{type(exc).__name__}: {exc}）")
        return 1
    if not top:
        print("[1] FAIL：引擎沒有回傳 top_logprobs，可能不支援 logprobs。")
        return 1
    kinds = [token for token, p in merged.items() if p > 0]
    print(f"[1] 引擎回了 {len(top)} 筆候選 token（要求 {s1.TOP_LOGPROBS} 筆），合併寫法後 {len(kinds)} 種：{top_tokens(merged, 5)}")
    if len(top) < s1.TOP_LOGPROBS:
        print("    回傳筆數比要求少：引擎可能有自己的上限，選項多的題目會讀不到後面的字母。")
    ok = len(kinds) >= 2
    print(f"[1] {'PASS' if ok else 'FAIL'}：" + ("有多個候選 token。" if ok else "只有一個候選 token，每個答案都會是 p=1 的假確定。"))

    n = min(10, s1.MAX_DIRECT_LABELS)
    if n < 9:
        print(f"\n[2] 略過：一題最多 {s1.MAX_DIRECT_LABELS} 個選項，I 不會是選項。")
        return 0 if ok else 1
    print(f"\n[2] 答不出來的題目，每題 {n} 個選項（A–{ascii_uppercase[n - 1]}）。欄位：字母合計  A  I  前三名")
    try:
        rows = []
        for state, question, texts in UNANSWERABLE:
            _, m, letters = read_letters(state, question, texts[:n])
            mass = sum(m.get(letter, 0.0) for letter in letters)
            others = [m.get(letter, 0.0) for letter in letters if letter not in ("A", "I")]
            rows.append((mass, m.get("A", 0.0), m.get("I", 0.0), mean(others)))
            print(f"    {mass:.2f}  {m.get('A', 0.0):.2f}  {m.get('I', 0.0):.2f}  {top_tokens(m)}")
        answered = sum(1 for row in rows if row[0] >= s1.MIN_LABEL_MASS)
        print(f"    {answered}/{len(rows)} 題的字母合計達到 MIN_LABEL_MASS={s1.MIN_LABEL_MASS:g}，端點會作答")
        print(f"    平均機率：A {mean([r[1] for r in rows]):.2f}，I {mean([r[2] for r in rows]):.2f}，"
              f"其他每個字母 {mean([r[3] for r in rows]):.2f}")
        hedge = [read_letters(state, question, texts[:8])[1].get("I", 0.0) for state, question, texts in UNANSWERABLE]
        print(f"[2] 同一批題目只給 8 個選項（I 不是選項）：I 平均 {mean(hedge):.2f}、最高 {max(hedge):.2f}。"
              "這是模型想用 “I …” 開頭說話的機率；選項 9 個以上時，這些機率會被讀成選 I。")
    except Exception as exc:
        print(f"[2] 中斷：引擎呼叫失敗（{type(exc).__name__}: {exc}）")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
