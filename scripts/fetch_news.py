#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sapporo-inshoku: 号外NET（札幌市中央区・北区）の「開店/閉店」カテゴリページから
最新記事の見出し・日付・リンクを自動収集し、news.json に保存するスクリプト。

- 外部ライブラリ不要（標準ライブラリのみ）で動作します。
- 記事の日付は本文の表記ではなく、記事URLに含まれる /YYYY/MM/DD/ から取得します
  （号外NETのパーマリンク構造は投稿日を含むため、これが最も安定します）。
- サイト構造が変わると抽出精度が落ちる可能性があります。その場合は
  LINK_RE の正規表現を調整してください。
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error

SOURCES = {
    "chuo": "https://sapporochuo.goguynet.jp/category/cat_openclose/",
    "kita": "https://sapporokitaku.goguynet.jp/category/cat_openclose/",
}

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


def clean_text(raw_html_fragment: str) -> str:
    text = TAG_RE.sub(" ", raw_html_fragment)
    text = text.replace("\xa0", " ")
    text = WS_RE.sub(" ", text).strip()
    # カテゴリラベルなど、記事一覧に混じりがちな定型ノイズを軽く除去
    for noise in ("開店/閉店", "NEW", "New"):
        if text == noise:
            return ""
    return text


def parse_articles(html: str, limit: int = MAX_ITEMS_PER_AREA):
    seen_urls = set()
    items = []
    for m in LINK_RE.finditer(html):
        url, year, month, day, raw_text = m.groups()
        title = clean_text(raw_text)
        # サムネイル画像だけのリンクや短すぎるテキストは記事タイトルとして扱わない
        if len(title) < 8:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        items.append({
            "title": title,
            "url": url,
            "date": f"{year}-{month}-{day}",
        })
        if len(items) >= limit:
            break
    return items


def main():
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+09:00", time.gmtime(time.time() + 9 * 3600)),
        "areas": {},
    }
    had_error = False
    for area, url in SOURCES.items():
        try:
            html = fetch_html(url)
            articles = parse_articles(html)
            result["areas"][area] = articles
            print(f"[{area}] {len(articles)} 件取得", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"[{area}] 取得失敗: {e}", file=sys.stderr)
            result["areas"][area] = []
            had_error = True

    with open("news.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")

    if had_error:
        # 一部失敗してもワークフロー自体は継続させたいので exit code は 0 のままにする
        print("一部のソースで取得に失敗しましたが、処理は継続しました。", file=sys.stderr)


if __name__ == "__main__":
    main()
