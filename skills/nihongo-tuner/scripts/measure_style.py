"""業務文書の中の、AI が書いたと気づかれやすい書き癖を数える。

使い方:
    python measure_style.py <ファイル...> [--json] [--list]

コードブロックの中は数えない（画面文言やログをそのまま写しているため）。
結果はファイル別の表と合計を出す。書き換えの前後で比べる用途を想定している。
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Windows の既定コンソール（cp932）では出力できない文字があるため UTF-8 に固定する
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

PATTERNS = {
    "太字": r"\*\*[^*\n]+\*\*",
    "括弧": r"（[^）\n]{1,80}）",
    "ダッシュ": r"[—―]|──",
    "矢印": r"→",
    "絵文字": r"[\U00010000-\U0010FFFF✅❌✔✖⚠]",
    "決め台詞": r"第一歩|中心テーマ|主目的です|ここがポイント|これで安心|ことが大切です|鍵を握",
    "空句": r"重要なのは|包括的|網羅的|多角的|掘り下げ|深掘り|において|の観点から|非常に|極めて",
    "開発者の言葉": r"を正と|が正[。）)、]|単一ソース|下流|上流|アンカー|黙って|縮退|走[らりるっ]",
    # 「A は B ではありません。C です。」の言い直しも数える
    "打ち消し": r"ではなく|わけでは(?:な|ありません)|では(?:ありません|ない)。[^。\n]{1,80}(?:です|だ|である)。",
    "冗長": r"ことができ|ことが可能|まず最初に|という形で",
    "です・ます": r"(?:です|ます|ください|ません)。",
    "だ・である": r"(?:である|だ|ない|する|した|いる|ある)。",
}


def strip_code(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    # 表の空欄を示す「—」だけのセルはダッシュの癖ではないので数えない
    return re.sub(r"\|\s*[—―\-]\s*(?=\|)", "|", text)


# 読点の目安。見直す文を拾う値で、規則ではない。
# 4 つは textlint-rule-max-ten の既定（1 文 3 つまで）に合わせた
COMMA_LONG_CHARS = 50
COMMA_MANY = 4
# 読点と読点のあいだの平均がこれより短いと、文節ごとに打っている疑いがある
COMMA_FINE_CHARS = 12
# この字数以下の文は読点が 0〜1 個で足り、主語・主題の直後は大きい切れ目にならない
COMMA_SHORT_CHARS = 30
# 主語・主題を受ける「は」「が」の直後の読点。直前がひらがなの「が」「は」は
# 逆接（「変更したが、」）や接続詞（「では、」）のことが多いので、「の」以外は数えない
SUBJECT_COMMA = re.compile(r"(?:[^ぁ-ゖ\s、。]|の)[はが]、")
# 主語がこの字数以上なら、述語との境目に読点を打ってよい
COMMA_SUBJECT_CHARS = 15


def has_short_subject_comma(sent: str) -> bool:
    """短い主語・主題の直後に読点があるか。"""
    for m in SUBJECT_COMMA.finditer(sent):
        head = sent[: m.end() - 1].rsplit("、", 1)[-1]
        head = re.sub(r"^[^:：]{1,20}[:：]\s*", "", head)  # 箇条書きのラベル
        if len(re.sub(r"\s", "", head)) < COMMA_SUBJECT_CHARS:
            return True
    return False


def sentences(prose: str) -> list[str]:
    """表・見出しを除いた本文を、句点と改行で文に分ける。"""
    body = re.sub(r"(?m)^#{1,6} .*$", "", prose)
    body = re.sub(r"`[^`\n]*`", "X", body)  # 識別子は 1 文字として数える
    body = re.sub(r"https?://\S+", "X", body)
    body = body.replace("**", "")
    out = []
    for line in body.splitlines():
        line = re.sub(r"^\s*(?:>\s*)?(?:[-*]|\d+\.)\s+", "", line).strip()
        for sent in re.split(r"(?<=。)", line):
            sent = sent.strip()
            if sent:
                out.append(sent)
    return out


def is_enumeration(sent: str) -> bool:
    """技術名や組織名を並べただけの行（句点で終わらない列挙）は文として扱わない。"""
    if sent.endswith("。"):
        return False
    body = re.sub(r"^[^:：]{1,20}[:：]\s*", "", sent)  # 「環境:」などのラベル
    if body.count("／") >= 2:
        return True
    parts = body.split("、")
    return len(parts) >= 3 and all(len(p) <= 30 for p in parts)


# 読点で区切った語句が名詞で終わっているか
NOUN_ITEM = re.compile(r"[^ぁ-ゖ\s。]$")
# 列挙の最後の語句は「経費の登録は」「担当者に連絡した。」のように、名詞のあとに助詞が続く形も認める。
# 助詞の前は 2 字以上とし、「念のため」の「念の」は名詞＋助詞とみなさない
NOUN_THEN_PARTICLE = re.compile(
    r"^.{1,19}?[^ぁ-ゖ\s](?:など|の|を|が|は|と|も|や|で|に)"
)
LIST_RUN = 3  # 名詞で終わる語句がこの数以上続いたら列挙とみなす


def counted_commas(sent: str) -> int:
    """名詞を並べただけの読点を除いた読点の数。

    名詞で終わる語句が 3 つ以上続く箇所（「列数、件数、行番号」）の読点は列挙として数えない。
    2 つまでの並び（「昨日、確認後、念のため、」の「昨日、」）は文の区切りとして数える。
    """
    items = [s.strip().rstrip("。") for s in sent.split("、")]
    if len(items) < 2:
        return 0
    listed, run = 0, 0
    for s in items + [""]:
        if s and len(s) <= 20 and NOUN_ITEM.search(s):
            run += 1
            continue
        # 名詞が続いたあとの、助詞で受ける語句までを列挙に含める
        if run and NOUN_THEN_PARTICLE.match(s):
            run += 1
        if run >= LIST_RUN:
            listed += run - 1
        run = 0
    return len(items) - 1 - listed


def comma_flags(prose: str) -> tuple[list[str], list[str], list[str], list[str]]:
    long_no_comma, many_commas, fine_commas, subject_commas = [], [], [], []
    for sent in sentences(prose):
        if is_enumeration(sent):
            continue
        length = len(re.sub(r"\s", "", sent))
        commas = counted_commas(sent)
        if length >= COMMA_LONG_CHARS and "、" not in sent:
            long_no_comma.append(sent)
        if commas >= COMMA_MANY:
            many_commas.append(sent)
        if commas >= 2 and length / (commas + 1) < COMMA_FINE_CHARS:
            fine_commas.append(sent)
        if length <= COMMA_SHORT_CHARS and has_short_subject_comma(sent):
            subject_commas.append(sent)
    return long_no_comma, many_commas, fine_commas, subject_commas


SENTENCE_PAREN = re.compile(r"（([^）\n]{1,120})）")
SENTENCE_END = re.compile(r"(?:です|ます|ません|ください|ためです|でした|ました)$|。")

LIST_ITEM = re.compile(r"(?m)^\s*(?:>\s*)?(?:[-*+]|\d+[.)])\s+(.+?)\s*$")
POLITE_END = re.compile(r"(?:です|ます|ません|ください|でした|ました|ましょう)$")


# だ・である調の 1 文で終わる項目の文末（動詞の終止形・「ない」・「た」・「だ」）。
# 漢字やカタカナで終わる語句（「ベータ版」「非対応」）は当たらない
PLAIN_END = re.compile(r"(?:[うくぐすずつづぬぶぷむゆる]|ない|た|だ)$")
TAIL_PAREN = re.compile(r"\s*[（(][^（）()]*[）)]$")


def list_items(prose: str) -> list[tuple[int, str]]:
    """箇条書きの項目を、折り返した継続行まで含めて 1 項目ずつ、一覧の番号と組で返す。

    継続行は、項目の直後に続く、字下げした行（空行と次の項目の行は除く）とみなす。
    一覧の番号は、項目と継続行と空行だけが続くあいだは同じで、字下げの深さが違う項目
    （入れ子）と、ほかの行で区切られた項目は別の一覧として数える。
    """
    items: list[tuple[int, str]] = []
    current: list[str] | None = None
    group = key = 0
    groups: dict[int, int] = {}  # 字下げの深さ → 一覧の番号
    for line in prose.splitlines():
        m = LIST_ITEM.match(line)
        if m:
            if current is not None:
                items.append((key, "".join(current)))
            depth = len(line) - len(line.lstrip())
            # この項目より深い一覧は前の親の子なので、次の子は別の一覧として数え直す
            groups = {d: g for d, g in groups.items() if d <= depth}
            if depth not in groups:
                group += 1
                groups[depth] = group
            key = groups[depth]
            current = [m.group(1)]
        elif current is not None and line.strip() and line[:1] in (" ", "\t"):
            current.append(line.strip())
        else:
            if current is not None:
                items.append((key, "".join(current)))
            current = None
            if line.strip():
                groups = {}
    if current is not None:
        items.append((key, "".join(current)))
    return items


def unterminated_items(prose: str) -> list[str]:
    """文になっているのに句点で終わらない箇条書きの項目。

    途中に句点がある項目と、です・ます で終わる項目を文とみなす。
    同じ一覧に句点で終わる項目があれば、だ・である調の 1 文の項目（「了承をもらう」）も文とみなす。
    語句だけの項目（「ベータ版」）と、コロンで終わるラベルの項目は数えない。
    """
    # 太字の装飾を外してから文末を見る（「**保存します。**」は句点で終わっている）
    items = [
        (g, item, re.sub(r"\*\*|__", "", item).rstrip())
        for g, item in list_items(prose)
    ]
    terminated = {g for g, _, body in items if body.endswith("。")}
    found = []
    for g, item, body in items:
        if body.endswith(("。", ":", "：")):
            continue
        if "。" in body or POLITE_END.search(body):
            found.append(item)
        # 末尾の括弧の補足（「数える（太字、括弧など）」）は外して文末を見る
        elif g in terminated and PLAIN_END.search(TAIL_PAREN.sub("", body)):
            found.append(item)
    return found


def minority_endings(prose: str) -> list[str]:
    """です・ます と だ・である のうち、文の数が少ないほうで終わる文。

    少ないほうが 1 文でもあれば返す（割合の警告では拾えない、1 文だけ混ざる形を見るため）。
    数が同じときは両方とも返さない（どちらにそろえるかは手順 3 の既定で決める）。
    """
    polite, plain = [], []
    for sent in sentences(prose):
        if re.search(PATTERNS["です・ます"] + "$", sent):
            polite.append(sent)
        elif re.search(PATTERNS["だ・である"] + "$", sent):
            plain.append(sent)
    if not polite or not plain or len(polite) == len(plain):
        return []
    return polite if len(polite) < len(plain) else plain


# 和文の文字（かな・漢字・全角の句読点と括弧）。半角英数字とのあいだの空白を数える
JA_CHARS = "ぁ-ゖァ-ヺー一-鿿々〆〇、。「」『』（）・：〜"
JA_ALNUM_SPACE = re.compile(
    rf"(?<=[{JA_CHARS}])[ \t]+(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])[ \t]+(?=[{JA_CHARS}])"
)


def ja_alnum_spaces(text: str) -> list[str]:
    """和文と半角英数字のあいだに入れた半角の空白。前後の数文字を添えて返す。

    バッククォート内の識別子と URL は、まわりの空白ごと数えない（識別子の区切りとして
    空白を入れる書き方があるため、この指標とは分けて扱う）。リンクは表示される文字だけを見る。
    英字の語どうしの空白（「Claude Code」）は数えない。
    """
    body = re.sub(r"\[([^\]\n]*)\]\([^)\n]*\)", r"\1", text)
    body = re.sub(r"`[^`\n]*`", "\0", body)
    body = re.sub(r"https?://\S+", "\0", body)
    found = []
    for line in body.splitlines():
        for m in JA_ALNUM_SPACE.finditer(line):
            ctx = line[max(0, m.start() - 6) : m.end() + 6].replace("\0", "`…`")
            found.append(ctx.strip())
    return found


def measure(path: Path) -> dict:
    text = strip_code(path.read_text(encoding="utf-8"))
    row = {k: len(re.findall(p, text)) for k, p in PATTERNS.items()}
    row["行数"] = text.count("\n") + 1
    prose = re.sub(r"(?m)^\|.*\|$", "", text)  # 表の行を除いた本文
    # 文をまるごと括弧に入れた補足。本文の一文に出すべきもの
    row["文の括弧"] = sum(
        1 for m in SENTENCE_PAREN.finditer(prose) if SENTENCE_END.search(m.group(1))
    )
    # 太字が 3 つ以上ある節（見出しから次の見出しまで・表の外）。目安は 1 節に 1〜2 箇所
    sections = re.split(r"(?m)^#{1,6} ", prose)
    row["太字過多の節"] = sum(
        1 for s in sections if len(re.findall(PATTERNS["太字"], s)) >= 3
    )
    # 50 字以上で読点のない文、読点が 4 つ以上ある文、読点の間隔が短い文、
    # 短い文で主語の直後に読点がある文（表の外）
    long_no_comma, many_commas, fine_commas, subject_commas = comma_flags(prose)
    row["読点なし長文"] = len(long_no_comma)
    row["読点過多文"] = len(many_commas)
    row["読点が細かい文"] = len(fine_commas)
    row["短い文の主語読点"] = len(subject_commas)
    # 文になっているのに句点で終わらない箇条書きの項目（表の外）
    unterminated = unterminated_items(prose)
    row["句点の落ちた項目"] = len(unterminated)
    # 和文と半角英数字のあいだの空白（表の中も数える）
    spaces = ja_alnum_spaces(text)
    row["英数字の前後の空白"] = len(spaces)
    row["_list"] = {
        "英数字の前後の空白": spaces,
        "句点の落ちた項目": unterminated,
        "読点なし長文": long_no_comma,
        "読点過多文": many_commas,
        "読点が細かい文": fine_commas,
        "短い文の主語読点": subject_commas,
        "少ないほうの文末": minority_endings(prose),
    }
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--json", action="store_true", help="JSON で出力する")
    ap.add_argument("--list", action="store_true", help="読点の偏りがある文、句点の落ちた項目、少ないほうの文末の文、英数字の前後の空白を一覧する")
    args = ap.parse_args()

    rows = {}
    for f in args.files:
        p = Path(f)
        if not p.is_file():
            print(f"warning: {f} が見つかりません", file=sys.stderr)
            continue
        rows[str(p)] = measure(p)

    keys = (
        ["行数"]
        + list(PATTERNS)
        + [
            "文の括弧",
            "太字過多の節",
            "読点なし長文",
            "読点過多文",
            "読点が細かい文",
            "短い文の主語読点",
            "句点の落ちた項目",
            "英数字の前後の空白",
        ]
    )
    total = {k: sum(r[k] for r in rows.values()) for k in keys}
    lists = {name: r.pop("_list") for name, r in rows.items()}

    if args.json:
        out = {"files": rows, "total": total}
        if args.list:
            out["list"] = lists
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print("\t".join(keys + ["ファイル"]))
    for name, r in sorted(rows.items(), key=lambda x: -x[1]["太字"]):
        print("\t".join(str(r[k]) for k in keys) + "\t" + Path(name).name)
    print("\t".join(str(total[k]) for k in keys) + "\t合計")
    mixed = [
        Path(n).name
        for n, r in rows.items()
        if min(r["です・ます"], r["だ・である"])
        > max(r["です・ます"], r["だ・である"]) * 0.2
    ]
    if mixed:
        print("\n文末が混在している可能性: " + ", ".join(mixed))
    if args.list:
        for name, found in lists.items():
            for kind, sents in found.items():
                for sent in sents:
                    print(f"\n[{kind}] {Path(name).name}\n  {sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
