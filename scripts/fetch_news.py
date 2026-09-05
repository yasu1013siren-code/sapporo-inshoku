#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sapporo-inshoku: 号外NET（札幌市の一部エリア）の「開店/閉店」カテゴリページから
最新記事の見出し・日付・リンクを自動収集し、news.json に保存するスクリプト。

- 外部ライブラリ不要（標準ライブラリのみ）で動作します。
- 記事の日付は本文の表記ではなく、記事URLに含まれる /YYYY/MM/DD/ から取得します
  （号外NETのパーマリンク構造は投稿日を含むため、これが最も安定します）。
- 号外NETは札幌市10区のうち「中央区」「北区」「西区・手稲区（合同）」のみ提供されているため、
  それ以外の6区（東区・白石区・厚別区・豊平区・清田区・南区）は自動収集の対象外です。
  対象外の区は news.json 上で空配列になります（index.html 側でその旨を表示します）。
- 記事タイトルの先頭についている「【札幌市◯◯区】」という表記から区を判定しているため、
  西区・手稲区が混在する sapporonishi-teine のページでも正しく振り分けられます。
- サイト構造が変わると抽出精度が落ちる可能性があります。その場合は
  LINK_RE の正規表現を調整してください。
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error

# 号外NETが提供しているカテゴリページ一覧（1ページに複数区が混在する場合もある）
SOURCE_URLS = [
    "https://sapporochuo.goguynet.jp/category/cat_openclose/",
    "https://sapporokitaku.goguynet.jp/category/cat_openclose/",
    "https://sapporonishi-teine.goguynet.jp/category/cat_openclose/",
]

# 記事タイトル冒頭の「【札幌市◯◯区】」からこのスクリプト内の区キーへのマッピング
WARD_NAME_TO_KEY = {
    "中央区": "chuo",
    "北区": "kita",
    "東区": "higashi",
    "白石区": "shiroishi",
    "厚別区": "atsubetsu",
    "豊平区": "toyohira",
    "清田区": "kiyota",
    "南区": "minami",
    "西区": "nishi",
    "手稲区": "teine",
}
ALL_AREA_KEYS = list(WARD_NAME_TO_KEY.values())
WARD_TAG_RE = re.compile(r"^【札幌市(.+?)】")

# 記事ページへのリンクとリンクテキストを抜き出す
LINK_RE = re.compile(
    r'<a[^>]+href="(https://[a-z0-9.\-]+\.goguynet\.jp/(\d{4})/(\d{2})/(\d{2})/[^"?#]+/?)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

MAX_ITEMS_PER_AREA = 20
TIMEOUT_SEC = 20
USER_AGENT = "Mozilla/5.0 (compatible; sapporo-inshoku-newsbot/1.0; +https://github.com/)"


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as res:
        raw = res.read()
    return raw.decode("utf-8", errors="replace")


# 記事一覧に付いてくるカテゴリラベル等（本文中には自然には出てこない語）
EDGE_LABELS = ("開店/閉店", "話題", "イベント", "まち", "お店News", "NEW", "New")


def strip_edge_labels(text: str) -> str:
    changed = True
    while changed:
        changed = False
        for label in EDGE_LABELS:
            if text.startswith(label):
                text = text[len(label):].strip()
                changed = True
            if text.endswith(label):
                text = text[: -len(label)].strip()
                changed = True
    return text


def clean_text(raw_html_fragment: str) -> str:
    text = TAG_RE.sub(" ", raw_html_fragment)
    text = text.replace("\xa0", " ")
    text = WS_RE.sub(" ", text).strip()
    if text in ("開店/閉店", "NEW", "New"):
        return ""
    # 「2026/09/03 07:01」のような投稿日時表記を、出現位置によらず除去
    text = re.sub(r"\d{4}/\d{2}/\d{2}\s*\d{1,2}:\d{2}", "", text)
    text = WS_RE.sub(" ", text).strip()
    # 記事一覧のカテゴリラベルを先頭・末尾から除去（複数付いている場合も繰り返し除去）
    text = strip_edge_labels(text)
    return text


# 飲食店の開店・閉店と無関係なノイズ（求人記事など）を除外するキーワード
NOISE_KEYWORDS = ("スタッフ募集", "アルバイト募集", "地域担当記者", "求人", "ライター募集")


def is_noise(title: str) -> bool:
    return any(kw in title for kw in NOISE_KEYWORDS)


# 見出しの文言から開店/閉店を推定するためのキーワード
OPEN_KEYWORDS = ("オープン", "OPEN", "NEW OPEN", "開店", "移転オープン", "リニューアルオープン", "リニューアル", "誕生")
CLOSED_KEYWORDS = ("閉店", "閉業", "幕を閉じ", "幕", "ラストオーダー", "営業終了", "休業")


def guess_type(title: str) -> str:
    # 「閉店」を含んでいても「〜跡地に新店オープン」のような開店寄りの文脈もあるため、
    # 先に閉店キーワードを優先判定（閉店告知の方が誤判定時の実害が小さいため）
    if any(kw in title for kw in CLOSED_KEYWORDS):
        return "closed"
    if any(kw in title for kw in OPEN_KEYWORDS):
        return "open"
    return "unknown"


def extract_ward(text: str):
    """先頭の「【札幌市◯◯区】」を取り除きつつ、対応する区キーを返す。
    見つからない場合は (None, 元のテキスト) を返す。"""
    m = WARD_TAG_RE.match(text)
    if not m:
        return None, text
    ward_name = m.group(1)
    remaining = text[m.end():].strip()
    remaining = strip_edge_labels(remaining)
    ward_key = WARD_NAME_TO_KEY.get(ward_name)
    return ward_key, remaining


def parse_articles(html: str):
    """HTMLから記事を抽出し、(区キー, 記事dict) のリストを返す。
    区が判定できなかった記事は除外する（誤ったタブに出さないための安全策）。"""
    seen_urls = set()
    results = []
    for m in LINK_RE.finditer(html):
        url, year, month, day, raw_text = m.groups()
        title_raw = clean_text(raw_text)
        # サムネイル画像だけのリンクや短すぎるテキストは記事タイトルとして扱わない
        if len(title_raw) < 8:
            continue
        if is_noise(title_raw):
            continue
        if url in seen_urls:
            continue
        ward_key, title = extract_ward(title_raw)
        if not ward_key or len(title) < 6:
            continue
        seen_urls.add(url)
        results.append((ward_key, {
            "title": title,
            "url": url,
            "date": f"{year}-{month}-{day}",
            "type": guess_type(title),
        }))
    return results


def main():
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+09:00", time.gmtime(time.time() + 9 * 3600)),
        "areas": {key: [] for key in ALL_AREA_KEYS},
    }
    by_area = {key: {} for key in ALL_AREA_KEYS}  # url -> item （区ごとの重複排除）
    had_error = False

    for url in SOURCE_URLS:
        try:
            html = fetch_html(url)
            parsed = parse_articles(html)
            print(f"[{url}] {len(parsed)} 件取得", file=sys.stderr)
            for ward_key, item in parsed:
                by_area[ward_key][item["url"]] = item
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"[{url}] 取得失敗: {e}", file=sys.stderr)
            had_error = True

    for key in ALL_AREA_KEYS:
        items = sorted(by_area[key].values(), key=lambda i: i["date"], reverse=True)
        result["areas"][key] = items[:MAX_ITEMS_PER_AREA]

    with open("news.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")

    if had_error:
        # 一部失敗してもワークフロー自体は継続させたいので exit code は 0 のままにする
        print("一部のソースで取得に失敗しましたが、処理は継続しました。", file=sys.stderr)


if __name__ == "__main__":
    main()
