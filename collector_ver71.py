# -*- coding: utf-8 -*-
"""
札幌圏＋道央4市 飲食店 開店・閉店 自動収集 Ver.7.3 深掘り収集
================================================
Ver.6系の「イベント収集」から分離し、札幌市10区＋千歳市・北広島市・苫小牧市・恵庭市の飲食店の
開店・閉店・開店予定を幅広い情報源から自動収集する専用コレクター。

主な特徴
- 札幌市公式「新規営業許可施設」を一次情報源として利用
- 号外NET / mogtrip / ショップス / 札幌速報 / 札幌リスト /
  SAPPOROYARD / 開店閉店.com / リビング札幌 / サツッター等を巡回
- 1サイトが落ちても最後まで処理
- URLだけでなく「店名＋区＋状態＋日付」を使って重複統合
- 同じ店を複数媒体が報じた場合、sources[] に情報源を統合
- 信頼度を自動算出
- GitHub Pages の index.html がそのまま読める news.json を出力
- SQLite に履歴を保存
- 新規発見だけ new_YYYY-MM-DD.csv に保存
- Instagram / X / TikTok / YouTube は公開索引（Google News RSS）のsite検索で深掘り
- SNS単独情報は低優先度で保持し、複数媒体との一致で信頼度を上げる
- SNS深掘り結果は social_signals.json にも分離保存

必要パッケージ:
    pip install requests beautifulsoup4 lxml
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
import urllib.robotparser
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlencode

import requests
from bs4 import BeautifulSoup

VERSION = "7.4"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "restaurants.db"
NEWS_PATH = BASE_DIR / "news.json"
CSV_PATH = DATA_DIR / f"new_{datetime.now():%Y-%m-%d}.csv"
LOG_PATH = BASE_DIR / "collector_ver71.log"
REPORT_JSON_PATH = BASE_DIR / "collector_ver71_report.json"
REPORT_CSV_PATH = DATA_DIR / f"collector_ver71_report_{datetime.now():%Y-%m-%d}.csv"

TIMEOUT = 25
RETRIES = 2
INTERVAL = 1.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/140 Safari/537.36 "
    "(SapporoInshokutenCollector/7.3 personal-use)"
)

HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ver74")

WARDS = {
    "中央区": "chuo",
    "北区": "kita",
    "東区": "higashi",
    "白石区": "shiroishi",
    "豊平区": "toyohira",
    "南区": "minami",
    "西区": "nishi",
    "厚別区": "atsubetsu",
    "手稲区": "teine",
    "清田区": "kiyota",
}

WARD_ALIASES = {
    "中央": "中央区", "北": "北区", "東": "東区", "白石": "白石区",
    "豊平": "豊平区", "南": "南区", "西": "西区", "厚別": "厚別区",
    "手稲": "手稲区", "清田": "清田区",
}

# 札幌市外の追加対象エリア
MUNICIPALITIES = {
    "千歳市": "chitose",
    "北広島市": "kitahiroshima",
    "苫小牧市": "tomakomai",
    "恵庭市": "eniwa",
}

AREA_MAP = {**WARDS, **MUNICIPALITIES}

# ---------------- 住所抽出 ----------------
# 「札幌市中央区南1条西2丁目3-4」のような和文住所表記をテキストからざっくり抜き出す。
# 完全ではないが、「最新ニュース速報」欄に場所のヒントを添えるには十分な精度を狙う。
_WARD_PATTERN = "|".join(list(WARDS.keys()) + list(MUNICIPALITIES.keys()))
ADDRESS_RE = re.compile(
    rf"(?:札幌市)?(?:{_WARD_PATTERN})"
    r"[^\s、。！!？?「」『』()（）\[\]\d]{0,12}"
    r"(?:\d+条(?:西|東)?\d*丁目(?:[\d\-−ー]+)?"
    r"|\d+丁目(?:[\d\-−ー]+)?"
    r"|[^\s、。！!？?「」『』()（）\[\]]{0,10}\d+条(?:西|東)?\d*"
    r"|[^\s、。！!？?「」『』()（）\[\]]{2,10})"
)


def extract_address(text: str) -> str:
    if not text:
        return ""
    m = ADDRESS_RE.search(text)
    if not m:
        return ""
    addr = m.group(0).strip()
    # 明らかに長すぎる／短すぎる誤爆はノイズとして捨てる
    if len(addr) < 4 or len(addr) > 40:
        return ""
    return addr

# 「飲食店」と判断するための語句。
FOOD_POSITIVE = [
    "飲食店", "レストラン", "食堂", "定食", "ラーメン", "そば", "うどん",
    "寿司", "鮨", "海鮮", "焼肉", "焼き肉", "ジンギスカン", "焼鳥", "焼き鳥",
    "居酒屋", "バー", "bar", "カフェ", "cafe", "喫茶", "コーヒー", "珈琲",
    "スイーツ", "ケーキ", "洋菓子", "和菓子", "パン", "ベーカリー", "ベーグル",
    "クレープ", "ピザ", "パスタ", "カレー", "スープカレー", "餃子",
    "弁当", "惣菜", "おにぎり", "ハンバーガー", "サンドイッチ", "韓国料理",
    "中華", "イタリアン", "フレンチ", "ワインバー", "ビストロ", "ダイニング",
    "立ち飲み", "立食い", "スープ", "ドーナツ", "アイス", "ジェラート",
    "ソフトクリーム", "チョコ", "ティースタンド", "タピオカ",
]

# 明らかな非飲食業態を落とす。食品を扱う物販は「飲食店」として断定しない。
FOOD_NEGATIVE = [
    "美容室", "美容院", "ネイル", "エステ", "サロン", "薬局", "ドラッグ",
    "病院", "クリニック", "歯科", "不動産", "ホテル", "旅館", "アパレル",
    "服", "雑貨", "家具", "家電", "書店", "本屋", "コンビニ", "スーパー",
    "ドラッグストア", "自動車", "車", "学習塾", "ジム", "フィットネス",
    "携帯ショップ", "スマホ", "宝飾", "アクセサリー", "ペット", "美容",
]

OPEN_WORDS = ["開店", "オープン", "OPEN", "open", "新店", "新規オープン", "開業", "出店", "リニューアルオープン", "移転オープン"]
CLOSED_WORDS = ["閉店", "営業終了", "営業を終了", "閉業", "閉鎖", "休業", "閉店へ", "閉店予定"]
UPCOMING_WORDS = ["オープン予定", "開店予定", "OPEN予定", "近日オープン", "近日OPEN", "オープンへ", "出店予定"]

SOURCE_CONFIG = [
    # 一次情報源
    {
        "id": "sapporo_official",
        "name": "札幌市公式・新規営業許可施設",
        "url": "https://www.city.sapporo.jp/hokenjo/shoku/shisetujouhou.html",
        "kind": "official_license",
        "priority": 100,
    },

    # 札幌ローカル開店閉店
    # default_status: ページ自体が「新店だけ」「閉店だけ」の一覧である場合、
    # 記事本文からステータス語（オープン/閉店）が拾えなくてもこの値を採用する。
    {"id": "mogtrip_open", "name": "mogtrip・新店", "url": "https://mogtrip.jp/newopen-2026/", "kind": "article_list", "priority": 90, "default_status": "open", "skip_food_check": True},
    {"id": "mogtrip_close", "name": "mogtrip・閉店", "url": "https://mogtrip.jp/closed-2026/", "kind": "article_list", "priority": 90, "default_status": "closed", "skip_food_check": True},
    {"id": "shopship", "name": "札幌ショップス・開店閉店", "url": "https://www.shopship.jp/sapporo/open-close/", "kind": "article_list", "priority": 90},
    {"id": "gogai_chuo", "name": "号外NET 札幌市中央区", "url": "https://sapporochuo.goguynet.jp/category/cat_openclose/", "kind": "gogai_list", "priority": 80},
    {"id": "gogai_kita", "name": "号外NET 札幌市北区", "url": "https://sapporokitaku.goguynet.jp/category/cat_openclose/", "kind": "gogai_list", "priority": 80},
    {"id": "gogai_nishi_teine", "name": "号外NET 札幌市西区・手稲区", "url": "https://sapporonishi-teine.goguynet.jp/category/cat_openclose/", "kind": "gogai_list", "priority": 80},
    {"id": "sapporo_sokuho_close", "name": "札幌速報・閉店", "url": "https://sapporo-sokuho.com/archives/category/%E9%96%8B%E5%BA%97%E3%83%BB%E9%96%89%E5%BA%97/%E9%96%89%E5%BA%97%E6%83%85%E5%A0%B1", "kind": "article_list", "priority": 80, "default_status": "closed"},
    {"id": "sapporo_list_open", "name": "札幌リスト・開店", "url": "https://sapporo-list.info/open/", "kind": "article_list", "priority": 75, "default_status": "open"},
    {"id": "sapporo_yard", "name": "SAPPOROYARD", "url": "https://sapporoyard.com/archives/openclose.html", "kind": "article_list", "priority": 70},
    {"id": "kaiten_heiten", "name": "開店閉店.com・札幌", "url": "https://kaiten-heiten-24.com/category/sapporo/", "kind": "article_list", "priority": 70},
    {"id": "teine_magazine", "name": "手稲マガジン・開店閉店", "url": "https://teine-magazine.com/category/kaitenheiten/", "kind": "article_list", "priority": 75, "force_ward": "手稲区"},

    # 札幌ショップスは区ごとに専用ページを持っている（/atsubetsu/open-close/ で確認済み）。
    # force_ward を指定して、区の取り違えが起きないようにする。
    # ※URLの区名（ローマ字）は厚別区のパターンから類推したもので、
    #   存在しない/形式が違うページは取得失敗として静かにスキップされるだけなので安全。
    {"id": "shopship_higashi", "name": "札幌ショップス・東区", "url": "https://www.shopship.jp/higashi/open-close/", "kind": "article_list", "priority": 85, "force_ward": "東区"},
    {"id": "shopship_shiroishi", "name": "札幌ショップス・白石区", "url": "https://www.shopship.jp/shiroishi/open-close/", "kind": "article_list", "priority": 85, "force_ward": "白石区"},
    {"id": "shopship_atsubetsu", "name": "札幌ショップス・厚別区", "url": "https://www.shopship.jp/atsubetsu/open-close/", "kind": "article_list", "priority": 85, "force_ward": "厚別区"},
    {"id": "shopship_toyohira", "name": "札幌ショップス・豊平区", "url": "https://www.shopship.jp/toyohira/open-close/", "kind": "article_list", "priority": 85, "force_ward": "豊平区"},
    {"id": "shopship_kiyota", "name": "札幌ショップス・清田区", "url": "https://www.shopship.jp/kiyota/open-close/", "kind": "article_list", "priority": 85, "force_ward": "清田区"},
    {"id": "shopship_minami", "name": "札幌ショップス・南区", "url": "https://www.shopship.jp/minami/open-close/", "kind": "article_list", "priority": 85, "force_ward": "南区"},
    {"id": "shopship_nishi", "name": "札幌ショップス・西区", "url": "https://www.shopship.jp/nishi/open-close/", "kind": "article_list", "priority": 85, "force_ward": "西区"},
    {"id": "shopship_teine", "name": "札幌ショップス・手稲区", "url": "https://www.shopship.jp/teine/open-close/", "kind": "article_list", "priority": 85, "force_ward": "手稲区"},
    {"id": "shopship_kita", "name": "札幌ショップス・北区", "url": "https://www.shopship.jp/kita/open-close/", "kind": "article_list", "priority": 85, "force_ward": "北区"},
    {"id": "shopship_chuo", "name": "札幌ショップス・中央区", "url": "https://www.shopship.jp/chuo/open-close/", "kind": "article_list", "priority": 85, "force_ward": "中央区"},
    {"id": "living_sapporo", "name": "リビング札幌Web・開店閉店", "url": "https://mrs.living.jp/sapporo/newopen", "kind": "article_list", "priority": 70},
    {"id": "satsutter", "name": "サツッター・新店舗", "url": "https://satsutter.com/tag/%E6%96%B0%E5%BA%97%E8%88%97%E3%82%AA%E3%83%BC%E3%83%97%E3%83%B3", "kind": "article_list", "priority": 65, "default_status": "open"},
    # 札幌圏4市：号外NET
    {"id": "gogai_chitose_eniwa_kitahiroshima", "name": "号外NET 千歳市・恵庭市・北広島市", "url": "https://chitose-eniwa-kitahiroshima.goguynet.jp/category/cat_openclose/", "kind": "gogai_city_list", "priority": 80},
    {"id": "gogai_tomakomai", "name": "号外NET 苫小牧市", "url": "https://tomakomai.goguynet.jp/category/cat_openclose/", "kind": "gogai_city_list", "priority": 80},
    # 札幌圏4市：ショップス
    {"id": "shopship_chitose", "name": "千歳ショップス・開店閉店", "url": "https://www.shopship.jp/chitose/open-close/", "kind": "article_list", "priority": 85, "force_ward": "千歳市"},
    {"id": "shopship_kitahiroshima", "name": "北広島ショップス・開店閉店", "url": "https://www.shopship.jp/kitahiroshima/open-close/", "kind": "article_list", "priority": 85, "force_ward": "北広島市"},
    {"id": "shopship_tomakomai", "name": "苫小牧ショップス・開店閉店", "url": "https://www.shopship.jp/tomakomai/open-close/", "kind": "article_list", "priority": 85, "force_ward": "苫小牧市"},
    {"id": "shopship_eniwa", "name": "恵庭ショップス・開店閉店", "url": "https://www.shopship.jp/eniwa/open-close/", "kind": "article_list", "priority": 85, "force_ward": "恵庭市"},
    # 札幌開店閉店インフォの4市カテゴリも巡回
    {"id": "chamonix_chitose", "name": "札幌開店閉店インフォ・千歳市", "url": "https://chamonix-cakes.com/category/%E6%96%B0%E5%BA%97%E6%83%85%E5%A0%B1/%E5%8D%83%E6%AD%B3%E5%B8%82%E3%81%AE%E6%96%B0%E5%BA%97%E6%83%85%E5%A0%B1/", "kind": "article_list", "priority": 65, "default_status": "open", "force_ward": "千歳市"},
    {"id": "chamonix_kitahiroshima", "name": "札幌開店閉店インフォ・北広島市", "url": "https://chamonix-cakes.com/category/%E9%96%89%E5%BA%97%E6%83%85%E5%A0%B1/%E5%8C%97%E5%BA%83%E5%B3%B6%E5%B8%82%E3%81%AE%E9%96%89%E5%BA%97%E6%83%85%E5%A0%B1/", "kind": "article_list", "priority": 65, "default_status": "closed", "force_ward": "北広島市"},
    {"id": "chamonix_eniwa", "name": "札幌開店閉店インフォ・恵庭市", "url": "https://chamonix-cakes.com/category/%E6%96%B0%E5%BA%97%E6%83%85%E5%A0%B1/%E6%81%B5%E5%BA%AD%E5%B8%82%E3%81%AE%E6%96%B0%E5%BA%97%E6%83%85%E5%A0%B1/", "kind": "article_list", "priority": 65, "default_status": "open", "force_ward": "恵庭市"},
    {"id": "chamonix", "name": "札幌開店閉店インフォ", "url": "https://chamonix-cakes.com/", "kind": "article_list", "priority": 65},
]

# 深掘り用の追加情報源。
DEEP_SOURCE_CONFIG = [
    {"id": "atorino_2026", "name": "ATORINO・札幌開店閉店2026", "url": "https://atorino-lab.com/sapporo-closing-opening-list-2026/", "kind": "article_list", "priority": 55},
    {"id": "kaiten_heiten_map_sapporo", "name": "開店閉店MAP・札幌", "url": "https://kaiten-heiten-map.com/city/%E5%8C%97%E6%B5%B7%E9%81%93%E6%9C%AD%E5%B9%8C%E5%B8%82", "kind": "article_list", "priority": 55},
    {"id": "hokkaidos_open_sapporo", "name": "北海道ねっと・札幌開店閉店", "url": "https://hokkaidos.net/open-sapporo/", "kind": "article_list", "priority": 50},
    {"id": "tabelog_job_opening_sapporo", "name": "食べログ求人・オープニングスタッフ", "url": "https://job.tabelog.com/search?condition=opening_staff_wanted&major_municipality=1100", "kind": "article_list", "priority": 40, "default_status": "upcoming", "signal_only": True},
]

# Google News公開RSSを使った検索エンジン型深掘り。
DEEP_SEARCH_TERMS = [
    '"オープン予定"', '"開店予定"', '"閉店予定"', '"オープニングスタッフ"',
    '"プレオープン"', '"新店舗"', '"移転オープン"', '"営業終了"', '"閉店"',
]

def build_deep_web_queries():
    queries = []
    for area_name in list(WARDS.keys()) + list(MUNICIPALITIES.keys()):
        loc = f"札幌市{area_name}" if area_name in WARDS else area_name
        for mode, terms in [("open", DEEP_SEARCH_TERMS[:7]), ("close", DEEP_SEARCH_TERMS[2:])]:
            expr = " OR ".join(terms)
            q = f'"{loc}" ({expr}) (飲食店 OR レストラン OR ラーメン OR カフェ OR 居酒屋 OR 焼肉 OR バー)'
            queries.append({"id": f"deepweb_{mode}_{AREA_MAP[area_name]}", "name": f"Web深掘り・{loc}・{mode}", "query": q, "force_ward": area_name, "priority": 35, "platform": "web_search"})
    return queries

DEEP_WEB_SEARCH_CONFIG = build_deep_web_queries()

# ---------------- SNS・マイナー情報の深掘り検索 ----------------
# Instagram / X / TikTok / YouTube の投稿ページを直接クロールするのではなく、
# Google News の公開RSSに「site:」検索をかけ、検索エンジンに公開・索引された
# SNS投稿や動画、ローカル記事を発見する方式。各SNSのrobots/仕様を直接回避しない。
# 取得できたものは「SNS検索シグナル」として低優先度で保存し、他媒体との一致で信頼度を上げる。
SOCIAL_PLATFORMS = {
    "instagram": "Instagram検索",
    "x": "X検索",
    "tiktok": "TikTok検索",
    "youtube": "YouTube検索",
}
SOCIAL_TERMS = [
    "開店", "オープン", "新店", "閉店", "営業終了", "移転", "移転オープン",
    "開業", "プレオープン", "近日オープン", "リニューアル", "休業",
]

def build_social_queries():
    queries = []
    # 札幌10区＋道央4市を個別検索。市区単位にすることで「札幌」の大きなノイズを減らす。
    for area_name in list(WARDS.keys()) + list(MUNICIPALITIES.keys()):
        if area_name in WARDS:
            loc = f"札幌市{area_name}"
            force = area_name
        else:
            loc = area_name
            force = area_name
        for platform, label in SOCIAL_PLATFORMS.items():
            domain = {
                "instagram": "instagram.com",
                "x": "x.com",
                "tiktok": "tiktok.com",
                "youtube": "youtube.com",
            }[platform]
            term_expr = " OR ".join(f'"{x}"' for x in SOCIAL_TERMS[:8])
            q = f'site:{domain} "{loc}" ({term_expr}) 飲食店'
            queries.append({
                "id": f"social_{platform}_{force}",
                "name": f"SNS深掘り・{label}・{loc}",
                "query": q,
                "force_ward": force,
                "priority": 45,
                "platform": platform,
            })
    return queries

SOCIAL_SOURCE_CONFIG = build_social_queries()
SOCIAL_SIGNALS: list[dict] = []
UNCONFIRMED_SIGNALS: list[dict] = []
SIGNAL_ITEMS: list[RestaurantItem] = []

session = requests.Session()
session.headers.update(HEADERS)

# ---------------- robots.txt 準拠チェック ----------------
# サイトの利用ルール（robots.txt）を必ず確認し、禁止されているサイトは
# 実際のページ取得を一切行わずにスキップする。判定できない場合も
# 安全側に倒してスキップする（＝許可が確認できたサイトだけ取得する）。
ROBOTS_CACHE: dict[str, "urllib.robotparser.RobotFileParser | None"] = {}


def is_allowed_by_robots(url: str) -> bool:
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base not in ROBOTS_CACHE:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
            ROBOTS_CACHE[base] = rp
        except Exception as e:
            log.warning("robots.txt を確認できませんでした: %s (%s) → 安全のためこのサイトはスキップします", base, e)
            ROBOTS_CACHE[base] = None
    rp = ROBOTS_CACHE[base]
    if rp is None:
        return False
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return False

# 実行時の詳細集計。
# source_id -> {scanned, link_candidates, accepted, rejected, reasons, wards, statuses, errors}
SOURCE_STATS = {}
REJECTION_EXAMPLES = []
MAX_REJECTION_EXAMPLES = 100

def init_source_stats(source: dict):
    SOURCE_STATS.setdefault(source["id"], {
        "id": source["id"],
        "name": source["name"],
        "url": source["url"],
        "scanned": 0,
        "link_candidates": 0,
        "accepted": 0,
        "rejected": 0,
        "errors": 0,
        "reasons": {},
        "wards": {},
        "statuses": {},
    })

def stat(source: dict, bucket: str, key: str, amount: int = 1):
    init_source_stats(source)
    if bucket in ("reasons", "wards", "statuses"):
        d = SOURCE_STATS[source["id"]][bucket]
        d[key] = d.get(key, 0) + amount
    else:
        SOURCE_STATS[source["id"]][bucket] += amount

def reject(source: dict, reason: str, title: str = "", href: str = ""):
    stat(source, "rejected", "") if False else None
    SOURCE_STATS[source["id"]]["rejected"] += 1
    d = SOURCE_STATS[source["id"]]["reasons"]
    d[reason] = d.get(reason, 0) + 1
    if len(REJECTION_EXAMPLES) < MAX_REJECTION_EXAMPLES:
        REJECTION_EXAMPLES.append({
            "source": source["name"],
            "reason": reason,
            "title": title[:180],
            "url": href,
        })

def accept(source: dict, item):
    stat(source, "accepted", "") if False else None
    SOURCE_STATS[source["id"]]["accepted"] += 1
    wards = SOURCE_STATS[source["id"]]["wards"]
    statuses = SOURCE_STATS[source["id"]]["statuses"]
    wards[item.ward or "区不明"] = wards.get(item.ward or "区不明", 0) + 1
    statuses[item.status or "unknown"] = statuses.get(item.status or "unknown", 0) + 1



@dataclass
class RestaurantItem:
    name: str
    ward: str = ""
    status: str = "unknown"          # open / closed / upcoming
    date: str = ""                   # YYYY-MM-DD または YYYY-MM / 未確定
    place: str = ""
    note: str = ""
    url: str = ""
    source: str = ""
    source_id: str = ""
    source_priority: int = 50
    first_seen: str = ""
    last_seen: str = ""
    confidence: float = 0.0
    sources: list = field(default_factory=list)


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = re.sub(r"\s+", "", s)
    return s


def fetch_text(url: str, timeout: int = TIMEOUT) -> str | None:
    for attempt in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=timeout)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or r.encoding
            return r.text
        except Exception as e:
            if attempt < RETRIES:
                time.sleep(2)
            else:
                log.warning("取得失敗: %s / %s", url, e)
        finally:
            time.sleep(INTERVAL)
    return None


def fetch_bytes(url: str) -> bytes | None:
    for attempt in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r.content
        except Exception as e:
            if attempt < RETRIES:
                time.sleep(2)
            else:
                log.warning("バイナリ取得失敗: %s / %s", url, e)
        finally:
            time.sleep(INTERVAL)
    return None


def parse_date(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    # 2026年9月5日 / 2026-09-05 / 2026/09/05
    m = re.search(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})日?", t)
    if m:
        try:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except ValueError:
            pass
    # YYYY年M月 / YYYY-MM
    m = re.search(r"(20\d{2})年(\d{1,2})月", t)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.search(r"(20\d{2})-(\d{1,2})", t)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    return ""


def detect_ward(text: str) -> str:
    t = norm(text)
    # 「北海道」「北海道新聞」等に含まれる「北」の1文字が、単独区名エイリアス
    # （WARD_ALIASESの"北"="北区"など）に誤マッチし続けていたための対策。
    # 都道府県名としての「北海道」は区の判定材料にならないため先に除去する。
    t = t.replace("北海道", "")
    for ward in WARDS:
        if norm(ward) in t:
            return ward
    # 1文字だけの略称（"北"="北区"など）は「北海道」のような無関係な単語にも
    # マッチしてしまい誤判定の原因になりやすいため、2文字以上の略称のみ使う。
    for alias, ward in WARD_ALIASES.items():
        if len(alias) >= 2 and norm(alias) in t:
            return ward
    # 札幌市外の追加対象4市
    for city in MUNICIPALITIES:
        if norm(city) in t:
            return city
    # 札幌の代表エリア → 区推定
    guesses = {
        "すすきの": "中央区", "大通": "中央区", "狸小路": "中央区", "円山": "中央区",
        "北24条": "北区", "麻生": "北区",
        "新琴似": "北区",
        "苗穂": "東区", "元町": "東区", "栄町": "東区",
        "菊水": "白石区", "南郷": "白石区", "白石": "白石区",
        "平岸": "豊平区", "月寒": "豊平区", "中の島": "豊平区",
        "真駒内": "南区", "澄川": "南区", "藻岩": "南区",
        "琴似": "西区", "発寒": "西区", "二十四軒": "西区",
        "新札幌": "厚別区", "大谷地": "厚別区", "厚別": "厚別区",
        "手稲": "手稲区", "星置": "手稲区", "稲穂": "手稲区",
        "清田": "清田区", "平岡": "清田区", "美しが丘": "清田区",
    }
    for key, ward in guesses.items():
        if norm(key) in t:
            return ward
    return ""


def is_food(text: str) -> bool:
    t = norm(text)
    if any(norm(x) in t for x in FOOD_NEGATIVE):
        # ただし「カフェ併設」「レストラン併設」などは文脈が複雑なので、
        # 飲食語が強く出ている場合は残す。
        positive = sum(1 for x in FOOD_POSITIVE if norm(x) in t)
        if positive < 2:
            return False
    return any(norm(x) in t for x in FOOD_POSITIVE)


def detect_status(text: str) -> str:
    t = text or ""
    if any(x in t for x in UPCOMING_WORDS):
        return "upcoming"
    if any(x in t for x in CLOSED_WORDS):
        return "closed"
    if any(x in t for x in OPEN_WORDS):
        return "open"
    return "unknown"


def extract_name_from_title(title: str) -> str:
    t = clean(title)
    patterns = [
        r"『(.+?)』",
        r"「(.+?)」",
        r"〖(.+?)〗",
    ]
    for p in patterns:
        m = re.search(p, t)
        if m:
            return clean(m.group(1))
    # よくある記事タイトルの先頭部分を軽く整理
    t = re.sub(r"^(?:【.*?】|\[.*?\])\s*", "", t)
    t = re.sub(r"^(?:札幌市[^ ]*区|札幌市)\s*", "", t)
    t = re.sub(r"\s*(?:が|は).{0,20}(?:オープン|OPEN|閉店|営業終了).*$", "", t, flags=re.I)
    return t[:100]


def get_item_context(a, title: str) -> str:
    """記事タイトルの日付・住所は、リンク（<a>）の直近の親ではなく、
    「1日 · 店名 · 住所」のように同じ行（li/p/tr/dd/divなど）の
    兄弟テキストとして存在することが多い（mogtripなど）。
    そのため、まず行全体を表すブロック要素までさかのぼってテキストを取得し、
    それが取れない・広すぎる場合は直近の親、最後はタイトルのみにフォールバックする。
    """
    block = a.find_parent(["li", "p", "tr", "dd", "dt"])
    if block is not None:
        block_text = clean(block.get_text(" ", strip=True))
        if block_text and len(block_text) <= 400:
            return block_text[:250]

    if a.parent is not None:
        parent_text = clean(a.parent.get_text(" ", strip=True))
        if parent_text and len(parent_text) <= 400:
            return parent_text[:250]

    return title


def article_candidates(source: dict, max_items: int = 120):
    """記事候補を広く走査し、除外理由を7.1の統計へ記録する。"""
    init_source_stats(source)

    if not is_allowed_by_robots(source["url"]):
        log.warning("robots.txtにより除外: %s (%s)", source["name"], source["url"])
        reject(source, "robots.txt禁止")
        return

    html = fetch_text(source["url"])
    if not html:
        SOURCE_STATS[source["id"]]["errors"] += 1
        reject(source, "取得失敗")
        return

    soup = BeautifulSoup(html, "lxml")
    seen = set()
    inspected = 0

    for a in soup.find_all("a", href=True):
        inspected += 1
        stat(source, "scanned", "") if False else None
        SOURCE_STATS[source["id"]]["scanned"] += 1

        title = clean(a.get_text(" ", strip=True))
        href = urljoin(source["url"], a["href"])
        if not title or len(title) < 4:
            reject(source, "タイトル短すぎ/空", title, href)
            continue
        if len(title) > 180:
            reject(source, "タイトル長すぎ", title, href)
            continue
        if href in seen:
            reject(source, "URL重複", title, href)
            continue
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            reject(source, "URL不正", title, href)
            continue
        if any(x in title.lower() for x in [
            "menu", "ログイン", "検索", "お問い合わせ", "お問合せ", "プライバシー",
            "home", "here", "姉妹サイト", "ライター紹介", "サイトマップ",
            "運営会社", "利用規約", "広告掲載", "delivery", "宅配弁当",
        ]):
            reject(source, "ナビゲーション/共通リンク", title, href)
            continue

        seen.add(href)
        SOURCE_STATS[source["id"]]["link_candidates"] += 1
        context = get_item_context(a, title)
        text = f"{title} {context}"
        status = detect_status(text)
        if status == "unknown" and not source.get("default_status"):
            reject(source, "開閉ステータス不明", title, href)
            continue
        if not source.get("skip_food_check") and not is_food(text):
            reject(source, "飲食店判定NG", title, href)
            continue

        yield title, href, context
        if SOURCE_STATS[source["id"]]["accepted"] >= max_items:
            break


def collect_article_source(source: dict):
    init_source_stats(source)
    for title, href, context in article_candidates(source):
        text = f"{title} {context}"
        status = detect_status(text)
        if status == "unknown" and source.get("default_status"):
            # このページ自体が「新店だけ」「閉店だけ」の一覧である場合の救済措置
            status = source["default_status"]
        name = extract_name_from_title(title)
        ward = source.get("force_ward") or detect_ward(text)
        d = parse_date(text)

        if not name:
            reject(source, "店名抽出失敗", title, href)
            continue

        item = RestaurantItem(
            name=name,
            ward=ward,
            status=status,
            date=d,
            place=extract_address(text),
            note=title,
            url=href,
            source=source["name"],
            source_id=source["id"],
            source_priority=source["priority"],
        )
        accept(source, item)
        yield item


def collect_sapporo_official():
    """
    札幌市「食品衛生関係施設情報」から最新の新規営業許可ZIPを探す。
    ZIP内のCSV/Excel-like textを読み、飲食店に該当する行を抽出する。
    市のページ自体が月次更新なので、過去月もSQLiteで保持できる。
    """
    source = next(x for x in SOURCE_CONFIG if x["id"] == "sapporo_official")
    init_source_stats(source)

    if not is_allowed_by_robots(source["url"]):
        log.warning("robots.txtにより除外: %s (%s)", source["name"], source["url"])
        reject(source, "robots.txt禁止")
        return

    html = fetch_text(source["url"])
    if not html:
        SOURCE_STATS[source["id"]]["errors"] += 1
        reject(source, "取得失敗")
        return

    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        txt = clean(a.get_text(" ", strip=True))
        href = urljoin(source["url"], a["href"])
        if "ZIP" in txt.upper() or href.lower().endswith(".zip"):
            if "令和8年" in txt or "令和7年" in txt or href.lower().endswith(".zip"):
                links.append((txt, href))

    # 新しい順に最大3ファイル。ページ上の並びを尊重。
    seen = set()
    selected = []
    for txt, href in links:
        if href in seen:
            continue
        seen.add(href)
        selected.append((txt, href))
        if len(selected) >= 3:
            break

    for label, href in selected:
        if not is_allowed_by_robots(href):
            log.warning("robots.txtによりZIP取得を除外: %s", href)
            reject(source, "robots.txt禁止(ZIP)")
            continue
        blob = fetch_bytes(href)
        if not blob:
            continue
        try:
            z = zipfile.ZipFile(io.BytesIO(blob))
        except Exception as e:
            log.warning("公式ZIPを開けません: %s / %s", href, e)
            continue

        for member in z.namelist():
            if member.endswith("/"):
                continue
            raw = z.read(member)
            # CSV/TSV/テキストを想定。文字コードは複数候補。
            text = None
            for enc in ("utf-8-sig", "cp932", "shift_jis", "utf-8"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    pass
            if not text:
                continue

            lines = text.splitlines()
            if len(lines) < 2:
                continue

            # CSV/TSVを柔軟に読む
            sample = "\n".join(lines[:10])
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
            except Exception:
                dialect = csv.excel_tab if "\t" in sample else csv.excel

            try:
                rows = list(csv.reader(io.StringIO(text), dialect))
            except Exception:
                continue

            header = [clean(x) for x in rows[0]]
            header_text = " ".join(header)
            if not any(k in header_text for k in ["施設", "営業", "所在地", "住所", "業種"]):
                continue

            for row in rows[1:]:
                SOURCE_STATS[source["id"]]["scanned"] += 1
                if not row:
                    reject(source, "空行")
                    continue
                joined = " ".join(clean(x) for x in row)
                if not joined:
                    reject(source, "空データ")
                    continue
                if not is_food(joined):
                    reject(source, "飲食店判定NG")
                    continue

                # 施設名・住所をヘッダ名から推定
                def find_col(keys):
                    for i, h in enumerate(header):
                        if any(k in h for k in keys) and i < len(row):
                            return clean(row[i])
                    return ""

                name = find_col(["施設名称", "施設名", "営業施設名称", "営業施設名", "名称"])
                address = find_col(["所在地", "住所"])
                kind = find_col(["業種", "営業種別", "営業の種類"])
                if not name:
                    # 行の先頭を保険として使う
                    name = clean(row[0])[:100]

                full = f"{name} {address} {kind} {joined}"
                ward = detect_ward(address or joined)
                d = parse_date(label) or parse_date(joined)

                if not name:
                    reject(source, "店名抽出失敗")
                    continue
                item = RestaurantItem(
                    name=name,
                    ward=ward,
                    status="open",
                    date=d,
                    place=address,
                    note=f"札幌市の新規営業許可施設（{label}）",
                    url=source["url"],
                    source=source["name"],
                    source_id=source["id"],
                    source_priority=100,
                )
                accept(source, item)
                yield item


def normalize_name(name: str) -> str:
    s = norm(name)
    s = re.sub(r"(札幌店|札幌本店|札幌駅店)$", "", s)
    s = re.sub(r"[『』「」【】（）()・,.，。/／\-‐–—_]", "", s)
    s = re.sub(r"\b\d+号店\b", "", s)
    return s


def event_key(item: RestaurantItem) -> str:
    n = normalize_name(item.name)
    ward = norm(item.ward)
    status = item.status
    # 日付は完全一致を要求しすぎない。店名＋区＋状態を基本キーにする。
    return f"{n}|{ward}|{status}"


def confidence(item: RestaurantItem, source_count: int = 1) -> float:
    score = 0.45
    if item.source_priority >= 100:
        score += 0.40
    elif item.source_priority >= 90:
        score += 0.25
    elif item.source_priority >= 80:
        score += 0.18
    elif item.source_priority >= 70:
        score += 0.12
    if item.ward:
        score += 0.05
    if item.date:
        score += 0.04
    if source_count >= 2:
        score += min(0.12, 0.04 * (source_count - 1))
    return round(min(score, 0.99), 2)


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS restaurants (
            event_key TEXT PRIMARY KEY,
            name TEXT,
            ward TEXT,
            status TEXT,
            date TEXT,
            place TEXT,
            note TEXT,
            url TEXT,
            source TEXT,
            sources_json TEXT,
            confidence REAL,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    conn.commit()


def merge_item(existing: RestaurantItem, item: RestaurantItem) -> RestaurantItem:
    # より情報量の多い値を優先
    if item.name and len(item.name) > len(existing.name):
        existing.name = item.name
    if not existing.ward and item.ward:
        existing.ward = item.ward
    if not existing.date and item.date:
        existing.date = item.date
    if not existing.place and item.place:
        existing.place = item.place
    if item.note and len(item.note) > len(existing.note):
        existing.note = item.note

    urls = {x.get("url") for x in existing.sources if x.get("url")}
    if item.url and item.url not in urls:
        existing.sources.append({
            "name": item.source,
            "url": item.url,
            "priority": item.source_priority,
        })

    existing.confidence = confidence(existing, len(existing.sources))
    existing.last_seen = datetime.now().strftime("%Y-%m-%d")
    return existing


def upsert(conn, item: RestaurantItem, today: str):
    key = event_key(item)
    row = conn.execute(
        "SELECT name, ward, status, date, place, note, url, source, sources_json, confidence, first_seen, last_seen "
        "FROM restaurants WHERE event_key = ?", (key,)
    ).fetchone()

    if row is None:
        sources = [{
            "name": item.source,
            "url": item.url,
            "priority": item.source_priority,
        }]
        item.sources = sources
        item.first_seen = today
        item.last_seen = today
        item.confidence = confidence(item, 1)
        conn.execute(
            "INSERT INTO restaurants VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key, item.name, item.ward, item.status, item.date, item.place,
                item.note, item.url, item.source, json.dumps(sources, ensure_ascii=False),
                item.confidence, today, today
            )
        )
        return True

    old = RestaurantItem(
        name=row[0] or "", ward=row[1] or "", status=row[2] or "unknown",
        date=row[3] or "", place=row[4] or "", note=row[5] or "",
        url=row[6] or "", source=row[7] or "", confidence=row[9] or 0,
        first_seen=row[10] or today, last_seen=row[11] or today,
    )
    try:
        old.sources = json.loads(row[8] or "[]")
    except Exception:
        old.sources = []
    merged = merge_item(old, item)
    conn.execute(
        "UPDATE restaurants SET name=?, ward=?, status=?, date=?, place=?, note=?, url=?, source=?, "
        "sources_json=?, confidence=?, first_seen=?, last_seen=? WHERE event_key=?",
        (
            merged.name, merged.ward, merged.status, merged.date, merged.place,
            merged.note, merged.url, merged.source,
            json.dumps(merged.sources, ensure_ascii=False),
            merged.confidence, merged.first_seen, merged.last_seen, key
        )
    )
    return False


def load_rows(conn):
    rows = conn.execute(
        "SELECT event_key,name,ward,status,date,place,note,url,source,sources_json,confidence,first_seen,last_seen "
        "FROM restaurants ORDER BY COALESCE(date,'9999-99-99'), name"
    ).fetchall()
    return rows


def build_news_json(conn, today: str):
    areas = {v: [] for v in AREA_MAP.values()}
    unmatched = []

    for row in load_rows(conn):
        key, name, ward, status, d, place, note, url, source, sources_json, conf, first_seen, last_seen = row
        area = AREA_MAP.get(ward)
        if not area:
            unmatched.append(row)
            continue

        try:
            sources = json.loads(sources_json or "[]")
        except Exception:
            sources = []

        # 現在から90日程度を中心に表示。日付不明は残す。
        item = {
            "name": name,
            "title": name,
            "date": d or "",
            "place": place or ward,
            "note": note or "",
            "type": status,
            "url": url or "",
            "source": source or "",
            "sources": sources,
            "confidence": conf,
            "source_count": len(sources),
            "first_seen": first_seen or "",
            "last_seen": last_seen or "",
        }
        areas[area].append(item)

    # 1区あたり最新150件程度（日付が新しい順。日付不明は末尾に回す）
    for area in areas:
        areas[area].sort(key=lambda x: x["date"] or "0000-00-00", reverse=True)
        areas[area] = areas[area][:150]

    payload = {
        "version": VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date": today,
        "areas": areas,
        "unmatched": len(unmatched),
        "source_count": len(SOURCE_CONFIG) + len(DEEP_SOURCE_CONFIG) + len(SOCIAL_SOURCE_CONFIG) + len(DEEP_WEB_SEARCH_CONFIG),
        "area_count": len(AREA_MAP),
        "unconfirmed": UNCONFIRMED_SIGNALS,
        "unconfirmed_count": len(UNCONFIRMED_SIGNALS),
        "areas_master": AREA_MAP,
        "deep_collection": {
            "social_source_count": len(SOCIAL_SOURCE_CONFIG),
            "social_signal_count": len(SOCIAL_SIGNALS),
            "social_platforms": list(SOCIAL_PLATFORMS.keys()),
            "deep_web_source_count": len(DEEP_WEB_SEARCH_CONFIG),
            "deep_fixed_source_count": len(DEEP_SOURCE_CONFIG),
            "unconfirmed_count": len(UNCONFIRMED_SIGNALS),
        },
    }
    NEWS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    social_path = BASE_DIR / "social_signals.json"
    social_path.write_text(json.dumps({
        "version": VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(SOCIAL_SIGNALS),
        "signals": SOCIAL_SIGNALS[:500],
        "unconfirmed_count": len(UNCONFIRMED_SIGNALS),
        "unconfirmed": UNCONFIRMED_SIGNALS[:500],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (BASE_DIR / "unconfirmed_signals.json").write_text(json.dumps({
        "version": VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(UNCONFIRMED_SIGNALS),
        "signals": UNCONFIRMED_SIGNALS[:500],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def save_new_csv(new_items):
    if not new_items:
        return
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["name", "ward", "status", "date", "place", "source", "url", "confidence"])
        for x in new_items:
            w.writerow([
                x.name, x.ward, x.status, x.date, x.place,
                x.source, x.url, x.confidence
            ])


def write_report(raw, merged, payload, today):
    ward_raw = {w: 0 for w in AREA_MAP}
    status_raw = {}
    unmatched_raw = 0
    for item in raw:
        if item.ward in ward_raw:
            ward_raw[item.ward] += 1
        else:
            unmatched_raw += 1
        status_raw[item.status] = status_raw.get(item.status, 0) + 1

    ward_final = {w: 0 for w in AREA_MAP}
    for w, area in AREA_MAP.items():
        ward_final[w] = len(payload["areas"].get(area, []))

    report = {
        "version": VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date": today,
        "summary": {
            "raw_candidates": len(raw),
            "merged_candidates": len(merged),
            "news_json_total": sum(len(v) for v in payload["areas"].values()),
            "social_sources": len(SOCIAL_SOURCE_CONFIG),
            "social_signals": len(SOCIAL_SIGNALS),
            "deep_signals": len(SIGNAL_ITEMS),
            "unconfirmed_signals": len(UNCONFIRMED_SIGNALS),
            "raw_unmatched_ward": unmatched_raw,
            "news_json_unmatched": payload.get("unmatched", 0),
            "status_raw": status_raw,
            "ward_raw": ward_raw,
            "ward_final": ward_final,
        },
        "sources": list(SOURCE_STATS.values()),
        "rejection_examples": REJECTION_EXAMPLES,
    }
    REPORT_JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with REPORT_CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["source", "scanned", "link_candidates", "accepted", "rejected", "errors", "reasons", "wards", "statuses"])
        for x in report["sources"]:
            w.writerow([
                x["name"], x["scanned"], x["link_candidates"], x["accepted"],
                x["rejected"], x["errors"], json.dumps(x["reasons"], ensure_ascii=False),
                json.dumps(x["wards"], ensure_ascii=False), json.dumps(x["statuses"], ensure_ascii=False),
            ])

    log.info("===== Ver.%s 詳細集計 =====", VERSION)
    log.info("候補: %d / 統合後: %d / JSON: %d / 区不明(raw): %d", len(raw), len(merged), sum(len(v) for v in payload["areas"].values()), unmatched_raw)
    log.info("--- サイト別 ---")
    for x in report["sources"]:
        log.info("%s | scan=%d link=%d 採用=%d 除外=%d error=%d | 理由=%s | 区=%s | status=%s",
                 x["name"], x["scanned"], x["link_candidates"], x["accepted"], x["rejected"], x["errors"],
                 x["reasons"], x["wards"], x["statuses"])
    log.info("--- 区別(raw採用) --- %s", ward_raw)
    log.info("--- 区別(JSON) --- %s", ward_final)
    log.info("--- ステータス(raw採用) --- %s", status_raw)
    if REJECTION_EXAMPLES:
        log.info("--- 除外サンプル（最大%d件） ---", MAX_REJECTION_EXAMPLES)
        for x in REJECTION_EXAMPLES[:30]:
            log.info("除外 | %s | %s | %s", x["source"], x["reason"], x["title"])
    log.info("詳細レポート: %s", REPORT_JSON_PATH)
    log.info("詳細CSV: %s", REPORT_CSV_PATH)
    return report


def fetch_google_news_rss(query: str):
    """Google News公開RSSから検索結果を取得。SNS本文そのものではなく公開索引を利用する。"""
    base = "https://news.google.com/rss/search"
    params = {"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"}
    return fetch_text(base + "?" + urlencode(params))


def collect_social_rss_source(source: dict):
    """SNSの公開索引を深掘りし、開店・閉店シグナルを抽出する。"""
    init_source_stats(source)
    rss_url = "https://news.google.com/rss/search"
    if not is_allowed_by_robots(rss_url):
        reject(source, "Google News robots.txt禁止")
        return

    xml = fetch_google_news_rss(source["query"])
    if not xml:
        SOURCE_STATS[source["id"]]["errors"] += 1
        reject(source, "RSS取得失敗")
        return

    soup = BeautifulSoup(xml, "xml")
    seen = set()
    for entry in soup.find_all("item"):
        title = clean(entry.find("title").get_text(" ", strip=True) if entry.find("title") else "")
        href = clean(entry.find("link").get_text(" ", strip=True) if entry.find("link") else "")
        desc = clean(entry.find("description").get_text(" ", strip=True) if entry.find("description") else "")
        pub = clean(entry.find("pubDate").get_text(" ", strip=True) if entry.find("pubDate") else "")
        text = clean(f"{title} {desc}")
        SOURCE_STATS[source["id"]]["scanned"] += 1
        if not title or not href or href in seen:
            reject(source, "タイトル/URL不正または重複", title, href)
            continue
        seen.add(href)
        SOURCE_STATS[source["id"]]["link_candidates"] += 1

        # Google News側の検索語だけで通すのではなく、本文/タイトルでも状態を再確認。
        status = detect_status(text)
        if status == "unknown":
            reject(source, "開閉ステータス不明", title, href)
            continue
        if not is_food(text):
            reject(source, "飲食店判定NG", title, href)
            continue

        # 検索対象エリアと本文の整合性を確認。強制エリアは「その区の検索」で得た結果に限定。
        force_ward = source.get("force_ward", "")
        detected = detect_ward(text)
        if force_ward and detected and detected != force_ward:
            reject(source, "対象エリア不一致", title, href)
            continue

        name = extract_name_from_title(title)
        if not name or len(name) < 2:
            reject(source, "店名抽出失敗", title, href)
            continue

        d = parse_date(text)
        if not d and pub:
            try:
                dt = parsedate_to_datetime(pub)
                d = dt.astimezone().strftime("%Y-%m-%d") if dt else ""
            except Exception:
                d = ""

        note = f"[SNS検索:{source.get('platform','')}] {title}"
        if desc:
            note += f" / {desc[:300]}"
        item = RestaurantItem(
            name=name,
            ward=force_ward or detected,
            status=status,
            date=d,
            place=extract_address(text),
            note=note,
            url=href,
            source=source["name"],
            source_id=source["id"],
            source_priority=source["priority"],
        )
        accept(source, item)
        SIGNAL_ITEMS.append(item)
        SOCIAL_SIGNALS.append({
            "platform": source.get("platform", ""),
            "area": force_ward or detected,
            "name": name,
            "type": status,
            "date": d,
            "title": title,
            "url": href,
            "source": source["name"],
        })


def collect_deep_web_source(source: dict):
    """一般Web検索の公開RSSから、小規模媒体・求人・店舗告知などを拾う。"""
    init_source_stats(source)
    rss_url = "https://news.google.com/rss/search"
    if not is_allowed_by_robots(rss_url):
        reject(source, "Google News robots.txt禁止")
        return
    xml = fetch_google_news_rss(source["query"])
    if not xml:
        SOURCE_STATS[source["id"]]["errors"] += 1
        reject(source, "RSS取得失敗")
        return
    soup = BeautifulSoup(xml, "xml")
    seen = set()
    for entry in soup.find_all("item"):
        title = clean(entry.find("title").get_text(" ", strip=True) if entry.find("title") else "")
        href = clean(entry.find("link").get_text(" ", strip=True) if entry.find("link") else "")
        desc = clean(entry.find("description").get_text(" ", strip=True) if entry.find("description") else "")
        pub = clean(entry.find("pubDate").get_text(" ", strip=True) if entry.find("pubDate") else "")
        text = clean(f"{title} {desc}")
        SOURCE_STATS[source["id"]]["scanned"] += 1
        if not title or not href or href in seen:
            reject(source, "タイトル/URL不正または重複", title, href)
            continue
        seen.add(href)
        SOURCE_STATS[source["id"]]["link_candidates"] += 1
        status = detect_status(text)
        if status == "unknown":
            reject(source, "開閉ステータス不明", title, href)
            continue
        if not is_food(text):
            reject(source, "飲食店判定NG", title, href)
            continue
        force = source.get("force_ward", "")
        detected = detect_ward(text)
        if force and detected and detected != force:
            reject(source, "対象エリア不一致", title, href)
            continue
        name = extract_name_from_title(title)
        if not name or len(name) < 2:
            reject(source, "店名抽出失敗", title, href)
            continue
        d = parse_date(text)
        if not d and pub:
            try:
                dt = parsedate_to_datetime(pub)
                d = dt.astimezone().strftime("%Y-%m-%d") if dt else ""
            except Exception:
                d = ""
        item = RestaurantItem(name=name, ward=force or detected, status=status, date=d, place=extract_address(text), note=f"[Web深掘り] {title} / {desc[:300]}", url=href, source=source["name"], source_id=source["id"], source_priority=source["priority"])
        accept(source, item)
        SIGNAL_ITEMS.append(item)


def collect_all():
    # 号外NET系（gogai_list）は記事URLに投稿日が入るパーマリンク構造と、
    # 記事タイトル先頭の「【札幌市◯◯区】」表記を使うと、汎用の<a>タグ走査より
    # はるかに高精度・高再現率で抽出できるため専用ロジックを使う。
    GOGAI_LINK_RE = re.compile(
        r'<a[^>]+href="(https://[a-z0-9.\-]+\.goguynet\.jp/(\d{4})/(\d{2})/(\d{2})/[^"?#]+/?)"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    GOGAI_TAG_STRIP_RE = re.compile(r"<[^>]+>")
    GOGAI_WS_RE = re.compile(r"\s+")
    GOGAI_WARD_TAG_RE = re.compile(r"^【札幌市(.+?)】")
    GOGAI_EDGE_LABELS = ("開店/閉店", "話題", "イベント", "まち", "お店News", "NEW", "New")

    def gogai_clean(raw_html_fragment: str) -> str:
        text = GOGAI_TAG_STRIP_RE.sub(" ", raw_html_fragment)
        text = text.replace("\xa0", " ")
        text = GOGAI_WS_RE.sub(" ", text).strip()
        text = re.sub(r"\d{4}/\d{2}/\d{2}\s*\d{1,2}:\d{2}", "", text)
        text = GOGAI_WS_RE.sub(" ", text).strip()
        changed = True
        while changed:
            changed = False
            for label in GOGAI_EDGE_LABELS:
                if text.startswith(label):
                    text = text[len(label):].strip()
                    changed = True
                if text.endswith(label):
                    text = text[: -len(label)].strip()
                    changed = True
        return text

    def collect_gogai_source(source: dict):
        init_source_stats(source)
        if not is_allowed_by_robots(source["url"]):
            log.warning("robots.txtにより除外: %s (%s)", source["name"], source["url"])
            reject(source, "robots.txt禁止")
            return
        html = fetch_text(source["url"])
        if not html:
            SOURCE_STATS[source["id"]]["errors"] += 1
            reject(source, "取得失敗")
            return

        seen_urls = set()
        for m in GOGAI_LINK_RE.finditer(html):
            href, year, month, day, raw_text = m.groups()
            SOURCE_STATS[source["id"]]["scanned"] += 1
            title_raw = gogai_clean(raw_text)
            if len(title_raw) < 8:
                reject(source, "タイトル短すぎ/空", title_raw, href)
                continue
            if href in seen_urls:
                reject(source, "URL重複", title_raw, href)
                continue
            seen_urls.add(href)
            SOURCE_STATS[source["id"]]["link_candidates"] += 1

            wm = GOGAI_WARD_TAG_RE.match(title_raw)
            if not wm:
                reject(source, "区タグなし", title_raw, href)
                continue
            ward_name = wm.group(1)
            name_text = title_raw[wm.end():].strip()
            if ward_name not in WARDS:
                reject(source, "区名不明", title_raw, href)
                continue
            if len(name_text) < 6:
                reject(source, "タイトル短すぎ/空", name_text, href)
                continue

            status = detect_status(name_text)
            if status == "unknown":
                reject(source, "開閉ステータス不明", name_text, href)
                continue
            if not is_food(name_text):
                reject(source, "飲食店判定NG", name_text, href)
                continue

            name = extract_name_from_title(name_text)
            if not name:
                reject(source, "店名抽出失敗", name_text, href)
                continue

            item = RestaurantItem(
                name=name,
                ward=ward_name,
                status=status,
                date=f"{year}-{month}-{day}",
                place=extract_address(name_text),
                note=name_text,
                url=href,
                source=source["name"],
                source_id=source["id"],
                source_priority=source["priority"],
            )
            accept(source, item)
            yield item

    def collect_gogai_city_source(source: dict):
        """号外NETの市単位ページ。タイトル先頭の【千歳市】等から対象市を判定。"""
        init_source_stats(source)
        if not is_allowed_by_robots(source["url"]):
            log.warning("robots.txtにより除外: %s (%s)", source["name"], source["url"])
            reject(source, "robots.txt禁止")
            return
        html = fetch_text(source["url"])
        if not html:
            SOURCE_STATS[source["id"]]["errors"] += 1
            reject(source, "取得失敗")
            return

        seen_urls = set()
        # 市外の別地域リンクを拾わないため、北海道の対象4市タグだけを採用
        city_tag_re = re.compile(r"^【(千歳市|恵庭市|北広島市|苫小牧市)】")
        for m in GOGAI_LINK_RE.finditer(html):
            href, year, month, day, raw_text = m.groups()
            SOURCE_STATS[source["id"]]["scanned"] += 1
            title_raw = gogai_clean(raw_text)
            if len(title_raw) < 8:
                reject(source, "タイトル短すぎ/空", title_raw, href)
                continue
            if href in seen_urls:
                reject(source, "URL重複", title_raw, href)
                continue
            seen_urls.add(href)
            SOURCE_STATS[source["id"]]["link_candidates"] += 1

            cm = city_tag_re.match(title_raw)
            if not cm:
                reject(source, "市タグなし", title_raw, href)
                continue
            city_name = cm.group(1)
            name_text = title_raw[cm.end():].strip()
            status = detect_status(name_text)
            if status == "unknown":
                reject(source, "開閉ステータス不明", name_text, href)
                continue
            if not is_food(name_text):
                reject(source, "飲食店判定NG", name_text, href)
                continue
            name = extract_name_from_title(name_text)
            if not name:
                reject(source, "店名抽出失敗", name_text, href)
                continue

            item = RestaurantItem(
                name=name, ward=city_name, status=status,
                date=f"{year}-{month}-{day}",
                place=extract_address(name_text),
                note=name_text, url=href,
                source=source["name"], source_id=source["id"],
                source_priority=source["priority"],
            )
            accept(source, item)
            yield item

    # まずSNS公開索引を深掘り。低優先度でDBへ入れ、他媒体との一致で信頼度を上げる。
    for source in SOCIAL_SOURCE_CONFIG:
        log.info("=== %s ===", source["name"])
        try:
            yield from collect_social_rss_source(source)
        except Exception as e:
            log.exception("%s でエラー: %s", source["name"], e)

    for source in DEEP_WEB_SEARCH_CONFIG:
        log.info("=== %s ===", source["name"])
        try:
            collect_deep_web_source(source)
        except Exception as e:
            log.exception("%s でエラー: %s", source["name"], e)

    for source in DEEP_SOURCE_CONFIG:
        log.info("=== %s ===", source["name"])
        try:
            if source.get("signal_only"):
                for item in collect_article_source(source):
                    SIGNAL_ITEMS.append(item)
            else:
                yield from collect_article_source(source)
        except Exception as e:
            log.exception("%s でエラー: %s", source["name"], e)

    for source in SOURCE_CONFIG:
        if source["kind"] == "official_license":
            log.info("=== %s ===", source["name"])
            try:
                yield from collect_sapporo_official()
            except Exception as e:
                log.exception("公式データ収集中にエラー: %s", e)
            continue

        if source["kind"] == "gogai_city_list":
            log.info("=== %s ===", source["name"])
            try:
                yield from collect_gogai_city_source(source)
            except Exception as e:
                log.exception("%s でエラー: %s", source["name"], e)
            continue

        if source["kind"] == "gogai_list":
            log.info("=== %s ===", source["name"])
            try:
                yield from collect_gogai_source(source)
            except Exception as e:
                log.exception("%s でエラー: %s", source["name"], e)
            continue

        log.info("=== %s ===", source["name"])
        try:
            yield from collect_article_source(source)
        except Exception as e:
            # 1サイトの失敗で全体を止めない
            log.exception("%s でエラー: %s", source["name"], e)


def signal_match_score(signal: RestaurantItem, item: RestaurantItem) -> int:
    """深掘りシグナルと通常情報を店名・住所・エリアで照合する。"""
    if signal.ward and item.ward and signal.ward != item.ward:
        return 0
    a = normalize_name(signal.name)
    b = normalize_name(item.name)
    if not a or not b:
        return 0
    score = 0
    if a == b:
        score += 80
    elif a in b or b in a:
        score += 55
    else:
        at = set(re.findall(r"[\wぁ-んァ-ヶ一-龥]{2,}", a))
        bt = set(re.findall(r"[\wぁ-んァ-ヶ一-龥]{2,}", b))
        if len(at & bt) >= 2:
            score += 35
    if signal.place and item.place:
        sa = norm(signal.place); sb = norm(item.place)
        if sa and sb and (sa in sb or sb in sa):
            score += 20
    if signal.status == item.status:
        score += 10
    return score


def process_deep_signals(merged: dict):
    """SNS/Web深掘りを通常情報と照合。未一致は未確認情報として別枠保存。"""
    for signal in SIGNAL_ITEMS:
        best_key = None; best_score = 0
        for key, item in merged.items():
            score = signal_match_score(signal, item)
            if score > best_score:
                best_key, best_score = key, score
        if best_key is not None and best_score >= 55:
            item = merged[best_key]
            item.sources = item.sources or []
            if signal.url and not any(x.get("url") == signal.url for x in item.sources):
                item.sources.append({"name": signal.source, "url": signal.url, "priority": signal.source_priority, "signal": True})
            item.confidence = confidence(item, len(item.sources))
            item.note = (item.note + " / 深掘り照合済み").strip(" /")
        else:
            UNCONFIRMED_SIGNALS.append({"name": signal.name, "title": signal.name, "area": signal.ward, "type": signal.status, "date": signal.date, "place": signal.place, "note": signal.note, "url": signal.url, "source": signal.source, "confidence": 0.30, "matched": False})
    seen = set(); unique = []
    for x in UNCONFIRMED_SIGNALS:
        k = (normalize_name(x["name"]), x["area"], x["type"], x["url"])
        if k in seen: continue
        seen.add(k); unique.append(x)
    UNCONFIRMED_SIGNALS[:] = unique[:500]


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    SOURCE_STATS.clear()
    REJECTION_EXAMPLES.clear()
    SOCIAL_SIGNALS.clear()
    UNCONFIRMED_SIGNALS.clear()
    SIGNAL_ITEMS.clear()
    log.info("===== Sapporo Inshokuten Collector Ver.%s START =====", VERSION)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    raw = []
    checked = 0
    for item in collect_all():
        checked += 1
        if not item.name:
            continue
        # 区が取れないものも一旦保存。後から人間が確認できるようにする。
        item.confidence = confidence(item, 1)
        raw.append(item)

    log.info("候補取得件数: %d", len(raw))

    # 同一実行内で一次統合
    merged = {}
    for item in raw:
        k = event_key(item)
        if k not in merged:
            item.sources = [{
                "name": item.source,
                "url": item.url,
                "priority": item.source_priority,
            }]
            merged[k] = item
        else:
            merged[k] = merge_item(merged[k], item)

    process_deep_signals(merged)

    new_items = []
    for item in merged.values():
        if upsert(conn, item, today):
            new_items.append(item)

    conn.commit()

    payload = build_news_json(conn, today)
    save_new_csv(new_items)
    write_report(raw, merged, payload, today)
    conn.close()

    counts = {w: 0 for w in AREA_MAP}
    for area, items in payload["areas"].items():
        ward = next((k for k, v in AREA_MAP.items() if v == area), None)
        if ward:
            counts[ward] = len(items)

    log.info("DB新規: %d件 / 統合後候補: %d件 / news.json総件数: %d",
             len(new_items), len(merged), sum(len(v) for v in payload["areas"].values()))
    log.info("区別件数: %s", counts)
    log.info("===== Ver.%s END =====", VERSION)


if __name__ == "__main__":
    main()
