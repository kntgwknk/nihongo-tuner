"""文体の書き換えで事実が変わっていないかを、書き換え前（git の版）と比べて照合する。

使い方:
    python check_facts.py --base <git ref> <ファイル...>
    python check_facts.py --before <元のファイル> <直したファイル>

比べるもの:
    コードブロック   … ブロック単位で完全一致（空行も含む）
    識別子           … バッククォート内の文字列
    数値             … コードブロック外の数字（見出し番号と番号付き箇条書きの番号は除く）
    画面文言         … 「」で囲んだ語
    用語             … 地の文のカタカナ語（3 字以上）と英字の語。書き換え後に 1 回も出ない語を候補として出す。
                       「デプロイ」が「配る」になったような、独自の言い換えを見つけるため

    打ち消し・確信の度合い … 「ない」「ません」と、「必ず」「かもしれ」などの語の数。前後で増減したら候補として出す。
                       「使える」が「使えない」に、「かもしれない」が「である」になったような、意味の変化を見つけるため
それぞれ、書き換え前にあって後にないもの（消えた）と、後にだけあるもの（増えた）を出す。
重複した文を削った場合も「消えた」に出るので、残った箇所に同じ事実があるかを人が確かめる。
差分が 1 件でもあれば終了コード 1 を返す。用語の候補は推測なので、件数と終了コードに含めない。
"""

import argparse
import difflib
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

CODE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)

# 打ち消しの語尾。「少ない」のような形容詞も数えるが、前後の数の差だけを見るので候補として扱う
NEGATION_RE = re.compile(r"ない|なかった|ません|ず[、。にとも]")
# 確信の度合いを表す語。強める語と弱める語のどちらも、書き換えで増減したら候補にする
CERTAINTY_WORDS = (
    "必ず", "絶対", "確実", "間違いなく", "常に",
    "かもしれ", "だろう", "でしょう", "と思われ", "と考えられ", "おそらく", "たぶん",
    "可能性が", "ことがある", "場合がある", "ようだ", "ようです", "らしい",
)
SHOW_SENTENCES = 3  # 候補ごとに示す、書き換え前の文の数


def load_base(path: Path, ref: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path.parent), "show", f"{ref}:./{path.name}"],
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    return out.stdout.decode("utf-8").replace("\r\n", "\n")


def extract(text: str) -> dict[str, Counter]:
    text = text.replace("\r\n", "\n")
    blocks = Counter(b.strip("\n") for b in CODE_RE.findall(text))
    # 太字の付け外しは文体の直しそのものなので、照合の前に記号を取り除く
    body = CODE_RE.sub("", text).replace("**", "")
    idents = Counter(re.findall(r"`([^`\n]+)`", body))
    quotes = Counter(re.findall(r"「([^」\n]{1,60})」", body))
    lines = []
    for line in body.split("\n"):
        line = re.sub(r"^\s*#+\s*[\d.]*", "", line)  # 見出し番号
        line = re.sub(r"^\s*\d+\.\s", "", line)  # 番号付き箇条書き
        line = re.sub(r"§\s*[\d.]+", "", line)  # 節への参照
        line = re.sub(r"`[^`\n]+`", "", line)  # 識別子内の数字は識別子側で見る
        lines.append(line)
    numbers = Counter(re.findall(r"\d+(?:[.,:/-]\d+)*", "\n".join(lines)))
    # 地の文の用語。識別子と URL は別に見るか比べる意味がないので除く
    prose = re.sub(r"`[^`\n]+`", " ", body)
    prose = re.sub(r"https?://\S+", " ", prose)
    prose = re.sub(r"\]\([^)\n]*\)", "]", prose)  # Markdown のリンク先
    terms = Counter(re.findall(r"[ァ-ヴー]{3,}", prose))
    terms.update(re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*[A-Za-z0-9+#]", prose))
    return {
        "コードブロック": blocks,
        "識別子": idents,
        "数値": numbers,
        "画面文言": quotes,
        "用語": terms,
    }


def prose_sentences(text: str) -> list[str]:
    """コードブロックを除いた本文を、句点と改行で文に分ける。"""
    body = CODE_RE.sub("", text.replace("\r\n", "\n")).replace("**", "")
    return [s.strip() for line in body.split("\n") for s in re.split(r"(?<=。)", line) if s.strip()]


def meaning_counts(text: str) -> Counter:
    """打ち消しと、確信の度合いの語ごとの数。"""
    counts = Counter({w: text.count(w) for w in CERTAINTY_WORDS if w in text})
    # 「かもしれない」の「ない」は確信の度合いの側で数えるので、打ち消しからは外す
    negations = len(NEGATION_RE.findall(text.replace("かもしれない", "かもしれ")))
    if negations:
        counts["打ち消し"] = negations
    return counts


def describe(before: Counter, after: Counter) -> str:
    keys = sorted(set(before) | set(after), key=lambda k: (k != "打ち消し", k))
    parts = []
    for k in keys:
        if before[k] != after[k]:
            label = k if k == "打ち消し" else f"確信の度合い「{k}」"
            parts.append(f"{label} {before[k]} → {after[k]}")
    return " / ".join(parts)


def meaning_candidates(base: str, current: str) -> list[str]:
    """書き換えた文で、打ち消しや確信の度合いの語が増減したものを候補として返す。

    文の意味を変える書き換え（「使える」→「使えない」、「かもしれない」→「である」）を拾うため。
    前後の文を difflib で対応付け、書き換えた箇所ごとに数を比べる。文書全体の数だけを比べると、
    ある文で増えて別の文で減ったときに打ち消し合って見逃すため。語の数で見る推測なので、
    事実の差分とは分けて扱う。そのまま残った文と、まるごと足した文・削った文は比べない。
    """
    before, after = prose_sentences(base), prose_sentences(current)
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    pairs = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        if i2 - i1 == j2 - j1:
            # 文の数が同じなら 1 文ずつ比べる
            pairs.extend(([before[i]], [after[j]]) for i, j in zip(range(i1, i2), range(j1, j2)))
        else:
            # 文を分けたりまとめたりした箇所は、まとまりで比べる
            pairs.append((before[i1:i2], after[j1:j2]))
    out = []
    for old, new in pairs:
        diff = describe(meaning_counts("".join(old)), meaning_counts("".join(new)))
        if not diff:
            continue
        out.append(f"  [候補] {diff}")
        out.extend(f"      前: {short(s)}" for s in old[:SHOW_SENTENCES])
        out.extend(f"      後: {short(s)}" for s in new[:SHOW_SENTENCES])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--base", help="書き換え前の git ref（例: HEAD, origin/main）")
    src.add_argument(
        "--before", help="書き換え前のファイル（git で管理していないとき。対象は 1 ファイル）"
    )
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()
    if args.before and len(args.files) != 1:
        ap.error("--before を使うときは、直したファイルを 1 つだけ指定する")

    found = 0
    paraphrase = 0
    meaning_count = 0
    for f in args.files:
        p = Path(f)
        if args.before:
            base = Path(args.before).read_text(encoding="utf-8").replace("\r\n", "\n")
        else:
            base = load_base(p, args.base)
        if base is None:
            print(f"warning: {f} は {args.base} に存在しないため照合できません")
            continue
        current = p.read_text(encoding="utf-8")
        before, after = extract(base), extract(current)
        meaning = meaning_candidates(base, current)
        report = []
        for kind in before:
            if kind == "用語":
                continue
            lost = before[kind] - after[kind]
            added = after[kind] - before[kind]
            for item, n in lost.items():
                report.append(
                    f"  [消えた] {kind}: {short(item)}" + (f" ×{n}" if n > 1 else "")
                )
            for item, n in added.items():
                report.append(
                    f"  [増えた] {kind}: {short(item)}" + (f" ×{n}" if n > 1 else "")
                )
        # 用語は、書き換え後に 1 回も出てこない語だけを候補にする。重複を削って回数が減るのは普通なので数えない。
        # 候補は推測なので、事実の差分の件数と終了コードには含めない
        candidates = sorted(t for t in before["用語"] if t not in after["用語"])
        if report or candidates or meaning:
            print(f"{f}")
        if report:
            found += len(report)
            print("\n".join(report))
        if candidates:
            paraphrase += len(candidates)
            print("\n".join(f"  [候補] 言い換えの候補: {short(t)}" for t in candidates))
        if meaning:
            meaning_count += sum(1 for line in meaning if line.startswith("  [候補]"))
            print("\n".join(meaning))
    if paraphrase:
        print(f"\n言い換えの候補 {paraphrase} 件。元の語に戻すか、言い換えた理由を確かめる。")
    if meaning_count:
        print(
            f"\n打ち消し・確信の度合いの増減 {meaning_count} 件。"
            "文の意味（肯定と否定、言い切りの強さ）が変わっていないかを確かめる。"
            "打ち消しを肯定形に直しただけなら問題ない。"
        )
    if found:
        print(f"\n差分 {found} 件。1 件ずつ理由を確かめ、説明のつかないものは戻す。")
        return 1
    print("ok: 事実の差分なし")
    return 0


def short(s: str, n: int = 80) -> str:
    s = s.replace("\n", "⏎")
    return s if len(s) <= n else s[:n] + "…"


if __name__ == "__main__":
    sys.exit(main())
