# -*- coding: utf-8 -*-
"""
札幌市10区 飲食店 開店・閉店 自動収集 Ver.7
================================================
Ver.6系の「イベント収集」から分離し、札幌市10区の飲食店の
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
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

VERSION = "7.1"
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
    "(SapporoInshokutenCollector/7.1 personal-use)"
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
log = logging.getLogger("ver71")

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
    {"id": "mogtrip_open", "name": "mogtrip・新店", "url": "https://mogtrip.jp/newopen-2026/", "kind": "article_list", "priority": 90, "default_status": "open"},
    {"id": "mogtrip_close", "name": "mogtrip・閉店", "url": "https://mogtrip.jp/closed-2026/", "kind": "article_list", "priority": 90, "default_status": "closed"},
    {"id": "shopship", "name": "札幌ショップス・開店閉店", "url": "https://www.shopship.jp/sapporo/open-close/", "kind": "article_list", "priority": 90},
    {"id": "gogai_chuo", "name": "号外NET 札幌市中央区", "url": "https://sapporochuo.goguynet.jp/category/cat_openclose/", "kind": "gogai_list", "priority": 80},
    {"id": "gogai_kita", "name": "号外NET 札幌市北区", "url": "https://sapporokitaku.goguynet.jp/category/cat_openclose/", "kind": "gogai_list", "priority": 80},
    {"id": "gogai_nishi_teine", "name": "号外NET 札幌市西区・手稲区", "url": "https://sapporonishi-teine.goguynet.jp/category/cat_openclose/", "kind": "gogai_list", "priority": 80},
    {"id": "sapporo_sokuho_close", "name": "札幌速報・閉店", "url": "https://sapporo-sokuho.com/archives/category/%E9%96%8B%E5%BA%97%E3%83%BB%E9%96%89%E5%BA%97/%E9%96%89%E5%BA%97%E6%83%85%E5%A0%B1", "kind": "article_list", "priority": 80, "default_status": "closed"},
    {"id": "sapporo_list_open", "name": "札幌リスト・開店", "url": "https://sapporo-list.info/open/", "kind": "article_list", "priority": 75, "default_status": "open"},
    {"id": "sapporo_yard", "name": "SAPPOROYARD", "url": "https://sapporoyard.com/archives/openclose.html", "kind": "article_list", "priority": 70},
    {"id": "kaiten_heiten", "name": "開店閉店.com・札幌", "url": "https://kaiten-heiten-24.com/category/sapporo/", "kind": "article_list", "priority": 70},
    {"id": "living_sapporo", "name": "リビング札幌Web・開店閉店", "url": "https://mrs.living.jp/sapporo/newopen", "kind": "article_list", "priority": 70},
    {"id": "satsutter", "name": "サツッター・新店舗", "url": "https://satsutter.com/tag/%E6%96%B0%E5%BA%97%E8%88%97%E3%82%AA%E3%83%BC%E3%83%97%E3%83%B3", "kind": "article_list", "priority": 65, "default_status": "open"},
    {"id": "chamonix", "name": "札幌開店閉店インフォ", "url": "https://chamonix-cakes.com/", "kind": "article_list", "priority": 65},
]

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
    for ward in WARDS:
        if norm(ward) in t:
            return ward
    for alias, ward in WARD_ALIASES.items():
        if norm(alias) in t:
            return ward
    # 札幌の代表エリア → 区推定
    guesses = {
        "すすきの": "中央区", "大通": "中央区", "狸小路": "中央区", "円山": "中央区",
        "札幌駅": "北区", "北24条": "北区", "麻生": "北区",
        "新琴似": "北区", "北口": "北区",
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
        if any(x in title.lower() for x in ["menu", "ログイン", "検索", "お問い合わせ", "プライバシー"]):
            reject(source, "ナビゲーション/共通リンク", title, href)
            continue

        seen.add(href)
        SOURCE_STATS[source["id"]]["link_candidates"] += 1
        context = clean(a.parent.get_text(" ", strip=True))[:700] if a.parent else title
        text = f"{title} {context}"
        status = detect_status(text)
        if status == "unknown" and not source.get("default_status"):
            reject(source, "開閉ステータス不明", title, href)
            continue
        if not is_food(text):
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
        ward = detect_ward(text)
        d = parse_date(text)

        if not name:
            reject(source, "店名抽出失敗", title, href)
            continue

        item = RestaurantItem(
            name=name,
            ward=ward,
            status=status,
            date=d,
            place="",
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
    areas = {v: [] for v in WARDS.values()}
    unmatched = []

    for row in load_rows(conn):
        key, name, ward, status, d, place, note, url, source, sources_json, conf, first_seen, last_seen = row
        area = WARDS.get(ward)
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
        "source_count": len(SOURCE_CONFIG),
    }
    NEWS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
    ward_raw = {w: 0 for w in WARDS}
    status_raw = {}
    unmatched_raw = 0
    for item in raw:
        if item.ward in ward_raw:
            ward_raw[item.ward] += 1
        else:
            unmatched_raw += 1
        status_raw[item.status] = status_raw.get(item.status, 0) + 1

    ward_final = {w: 0 for w in WARDS}
    for w, area in WARDS.items():
        ward_final[w] = len(payload["areas"].get(area, []))

    report = {
        "version": VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "date": today,
        "summary": {
            "raw_candidates": len(raw),
            "merged_candidates": len(merged),
            "news_json_total": sum(len(v) for v in payload["areas"].values()),
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
                place="",
                note=name_text,
                url=href,
                source=source["name"],
                source_id=source["id"],
                source_priority=source["priority"],
            )
            accept(source, item)
            yield item

    for source in SOURCE_CONFIG:
        if source["kind"] == "official_license":
            log.info("=== %s ===", source["name"])
            try:
                yield from collect_sapporo_official()
            except Exception as e:
                log.exception("公式データ収集中にエラー: %s", e)
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


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    SOURCE_STATS.clear()
    REJECTION_EXAMPLES.clear()
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

    new_items = []
    for item in merged.values():
        if upsert(conn, item, today):
            new_items.append(item)

    conn.commit()

    payload = build_news_json(conn, today)
    save_new_csv(new_items)
    write_report(raw, merged, payload, today)
    conn.close()

    counts = {w: 0 for w in WARDS}
    for area, items in payload["areas"].items():
        ward = next((k for k, v in WARDS.items() if v == area), None)
        if ward:
            counts[ward] = len(items)

    log.info("DB新規: %d件 / 統合後候補: %d件 / news.json総件数: %d",
             len(new_items), len(merged), sum(len(v) for v in payload["areas"].values()))
    log.info("区別件数: %s", counts)
    log.info("===== Ver.%s END =====", VERSION)


if __name__ == "__main__":
    main()
