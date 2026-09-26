# -*- coding: utf-8 -*-
"""
札幌・道央 飲食店 開店閉店コレクター Ver.7.3.6
================================================
Ver.7.2 を確定情報ラインとしてそのまま実行した後、
公開Web上の「SNS痕跡・求人・地域記事・マイナー情報」を検索/RSS経由で深掘りし、
既存 news.json と照合して未確認情報を別ファイルへ分離する追加レイヤー。

重要:
- 確定情報のDBには未確認情報を混ぜない
- 江別市は対象外
- 対象: 札幌10区 + 千歳市 + 恵庭市 + 北広島市 + 苫小牧市
- 1情報源が失敗しても全体を止めない
- social_signals.json: 深掘りで取得した全候補
- unconfirmed_signals.json: 既存確定情報と十分一致しない候補
- news.json: Ver.7.2の内容を維持しつつ unconfirmed_* メタ情報を追加

必要:
    collector_ver72.py が同じフォルダにあること
    pip install requests beautifulsoup4 lxml
"""

from __future__ import annotations

import html
import importlib.util
import json
import logging
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import requests

VERSION = "7.3.6"
BASE_DIR = Path(__file__).resolve().parent
CORE_PATH = BASE_DIR / "collector_ver72.py"
NEWS_PATH = BASE_DIR / "news.json"
SOCIAL_PATH = BASE_DIR / "social_signals.json"
UNCONFIRMED_PATH = BASE_DIR / "unconfirmed_signals.json"
REPORT_PATH = BASE_DIR / "collector_ver736_report.json"
LOG_PATH = BASE_DIR / "collector_ver736.log"

TARGET_AREAS = [
    "中央区", "北区", "東区", "白石区", "豊平区", "南区", "西区",
    "厚別区", "手稲区", "清田区",
    "千歳市", "恵庭市", "北広島市", "苫小牧市",
]
EXCLUDED_AREAS = ["江別市", "江別"]

AREA_HINTS = {
    "中央区": ["中央区", "すすきの", "大通", "狸小路", "円山", "札幌駅南口"],
    "北区": ["北区", "札幌駅北口", "北24条", "麻生", "新琴似"],
    "東区": ["東区", "苗穂", "元町", "栄町"],
    "白石区": ["白石区", "菊水", "南郷"],
    "豊平区": ["豊平区", "平岸", "月寒", "中の島"],
    "南区": ["南区", "真駒内", "澄川", "藻岩"],
    "西区": ["西区", "琴似", "発寒", "二十四軒"],
    "厚別区": ["厚別区", "新札幌", "新さっぽろ", "大谷地"],
    "手稲区": ["手稲区", "手稲", "星置", "稲穂"],
    "清田区": ["清田区", "清田", "平岡", "美しが丘"],
    "千歳市": ["千歳市", "千歳駅", "新千歳空港"],
    "恵庭市": ["恵庭市", "恵庭駅"],
    "北広島市": ["北広島市", "北広島駅", "Fビレッジ", "エスコン"],
    "苫小牧市": ["苫小牧市", "苫小牧駅"],
}

FOOD_WORDS = [
    "飲食", "レストラン", "食堂", "定食", "ラーメン", "そば", "うどん",
    "寿司", "鮨", "海鮮", "焼肉", "ジンギスカン", "焼鳥", "焼き鳥",
    "居酒屋", "バー", "bar", "カフェ", "cafe", "喫茶", "コーヒー", "珈琲",
    "スイーツ", "ケーキ", "パン", "ベーカリー", "ピザ", "パスタ", "カレー",
    "餃子", "弁当", "惣菜", "おにぎり", "ハンバーガー", "韓国料理",
    "中華", "イタリアン", "フレンチ", "ビストロ", "ダイニング",
    "ドーナツ", "ジェラート", "ソフトクリーム", "キッチン", "店舗",
]
OPEN_WORDS = [
    "オープン予定", "開店予定", "近日オープン", "近日open", "new open",
    "ニューオープン", "新規オープン", "オープニング", "新店", "出店予定",
    "開業予定", "open予定", "オープン", "開店", "出店",
]
CLOSE_WORDS = [
    "閉店予定", "営業終了", "営業を終了", "閉業", "閉店", "閉鎖",
]
JOB_WORDS = [
    "オープニングスタッフ", "新店スタッフ", "新規オープンスタッフ",
    "オープン予定スタッフ", "新店オープン", "opening staff",
]
RUMOR_WORDS = ["らしい", "との情報", "予定", "計画", "求人", "募集", "オープニング"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/140 Safari/537.36 "
    "(SapporoInshokutenCollector/7.3 personal-use)"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"}
TIMEOUT = 20
INTERVAL = 0.4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ver736")
session = requests.Session()
session.headers.update(HEADERS)


@dataclass
class Signal:
    title: str
    url: str
    source: str
    source_type: str
    area: str = ""
    status: str = "unknown"
    store_name: str = ""
    address_hint: str = ""
    date_hint: str = ""
    snippet: str = ""
    first_seen: str = ""
    confidence: float = 0.0
    match_score: float = 0.0
    matched_store: str = ""
    matched_url: str = ""
    verification: str = "unconfirmed"
    candidate_type: str = "named_store"
    display_eligible: bool = False
    review_class: str = "C"
    review_label: str = "保存のみ"
    review_score: int = 0
    review_reasons: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", html.unescape(s or "")).casefold()
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", "", s)
    return s


def clean(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_core():
    if not CORE_PATH.exists():
        raise FileNotFoundError("collector_ver72.py が同じフォルダにありません。")
    spec = importlib.util.spec_from_file_location("collector_ver72", CORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["collector_ver72"] = mod
    spec.loader.exec_module(mod)
    return mod


def google_news_rss(query: str):
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=ja&gl=JP&ceid=JP:ja"
    )
    try:
        r = session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall(".//item"):
            title = clean(item.findtext("title") or "")
            link = clean(item.findtext("link") or "")
            desc = clean(item.findtext("description") or "")
            pub = clean(item.findtext("pubDate") or "")
            source_el = item.find("source")
            source = clean(source_el.text if source_el is not None else "")
            yield {
                "title": title,
                "url": link,
                "snippet": desc,
                "published": pub,
                "source": source or "Google News RSS",
            }
    except Exception as e:
        log.warning("RSS取得失敗: %s / %s", query, e)


OUTSIDE_HOKKAIDO_PLACE_PATTERNS = [
    r"浜松市(?:中央区|浜名区|天竜区)", r"江東区", r"東京都(?:中央区|北区|東区|南区|西区)",
    r"大阪市(?:中央区|北区|東区|南区|西区)", r"名古屋市(?:中区|北区|東区|南区|西区)",
    r"福岡市(?:中央区|博多区|東区|南区|西区)", r"神戸市(?:中央区|北区|東灘区|西区)",
    r"横浜市(?:中区|北区|西区|南区)", r"さいたま市(?:中央区|北区|西区|南区)",
    r"千葉市(?:中央区|花見川区|稲毛区|若葉区|緑区|美浜区)",
    r"相模原市(?:中央区|南区|緑区)", r"新潟市(?:中央区|北区|東区|南区|西区)",
    r"熊本市(?:中央区|北区|東区|南区|西区)", r"広島市(?:中区|東区|南区|西区)",
    r"岡山市(?:北区|中区|東区|南区)", r"堺市(?:北区|中区|東区|西区|南区)",
]

def has_target_location_context(text: str) -> bool:
    t = clean(text)
    if any(re.search(p, t) for p in OUTSIDE_HOKKAIDO_PLACE_PATTERNS):
        # 対象地名も明記されている記事（例: 新千歳空港の味が東京へ）は、出店先が道外なら除外。
        if not re.search(r"(?:北海道)?(?:札幌市|千歳市|恵庭市|北広島市|苫小牧市)", t):
            return False
        # 見出し冒頭の【江東区】等は記事の主対象地域として優先。
        if re.search(r"^【?(?:江東区|浜松市|東京都|大阪市|名古屋市|福岡市|神戸市|横浜市|さいたま市)", t):
            return False
    return True

def target_geo_consistent(text: str, area: str) -> bool:
    """Ver.7.3.6: A掲載前の厳格な地域整合性確認。全国にある区名単独を札幌扱いしない。"""
    t = clean(text)
    if not area or area == "札幌市・区不明":
        return False
    if not has_target_location_context(t):
        return False

    # 札幌10区は、札幌市の明示か札幌固有地名が必要。
    sapporo_wards = {"中央区", "北区", "東区", "白石区", "豊平区", "南区", "西区", "厚別区", "手稲区", "清田区"}
    if area in sapporo_wards:
        if re.search(r"(?:北海道)?札幌市", t):
            return True
        unique_hints = [h for h in AREA_HINTS.get(area, []) if h != area]
        return any(norm(h) in norm(t) for h in unique_hints)

    # 4市は市名または強い固有地名を要求。
    return any(norm(h) in norm(t) for h in AREA_HINTS.get(area, []))


def detect_area(text: str) -> str:
    t0 = clean(text)
    t = norm(t0)
    if any(norm(x) in t for x in EXCLUDED_AREAS):
        return "__excluded__"
    if not has_target_location_context(t0):
        return "__outside__"

    # 「中央区」「東区」単独は全国に存在するため、札幌の明示または札幌固有地名を要求する。
    sapporo_explicit = bool(re.search(r"(?:北海道)?札幌市", t0))
    for area, hints in AREA_HINTS.items():
        for hint in hints:
            if norm(hint) not in t:
                continue
            if area in {"中央区", "北区", "東区", "南区", "西区"} and hint == area and not sapporo_explicit:
                continue
            return area
    if "札幌" in t0:
        return "札幌市・区不明"
    return ""


def detect_status(text: str) -> str:
    t = norm(text)
    if any(norm(x) in t for x in CLOSE_WORDS):
        return "closed"
    if any(norm(x) in t for x in OPEN_WORDS):
        return "upcoming"
    return "unknown"


def is_food_signal(text: str) -> bool:
    t = norm(text)
    return any(norm(x) in t for x in FOOD_WORDS)


def source_type(url: str, text: str, source: str = "") -> str:
    """媒体種別を、本文の開店語より媒体名/ドメインを優先して判定する。"""
    host = urlparse(url).netloc.casefold()
    tx = norm(text)
    src = norm(source)
    joined = f"{host} {src}"

    if "instagram" in joined or "instagram.com" in tx:
        return "instagram"
    if "tiktok" in joined:
        return "tiktok"
    if "twitter" in joined or re.search(r"(?:^|\.)x\.com$", host):
        return "x"
    if any(x in joined for x in ["prtimes", "pr times", "atpress", "value-press", "dreamnews"]):
        return "press_release"
    if any(x in joined for x in [
        "indeed", "townwork", "baitoru", "froma", "engage", "求人ボックス",
        "求人", "マイナビバイト", "バイトル", "タウンワーク"
    ]):
        return "job"

    # 媒体が判別できない時だけ、明確な募集表現を補助判定に使う。
    if any(norm(x) in tx for x in [
        "オープニングスタッフ募集", "新店スタッフ募集",
        "新規オープンスタッフ募集", "opening staff"
    ]):
        return "job"
    return "web"


BAD_STORE_NAMES = {
    "場所", "明日", "本日", "今日", "新店", "新店舗", "新店情報", "閉店情報",
    "新店オープン", "新規オープン", "オープン", "open", "opening",
    "開店", "閉店", "開店予定", "閉店予定", "オープン予定", "店舗", "飲食店",
    "すすきの", "大通", "狸小路", "円山", "札幌", "札幌市", "千歳", "千歳市",
    "恵庭", "恵庭市", "北広島", "北広島市", "苫小牧", "苫小牧市",
    "すすきのに新店", "札幌新店", "札幌新店居酒屋", "新店居酒屋",
    "ご挨拶", "ニューオープン", "newopen", "新規開店", "開業", "お知らせ",
    "オープン情報", "開店情報", "札幌ニューオープン", "新店紹介",
    "募集職種", "店舗概要", "会社概要", "求人情報", "採用情報", "スタッフ募集",
    "いよいよ本日", "いよいよ本日オープン", "本日グランドオープン",
    "オープニングスタッフ", "スタッフ募集", "求人募集", "新店舗スタッフ",
    "新店舗オープン", "グランドオープン", "新店オープニングスタッフ",
}

BAD_STORE_FRAGMENTS = [
    "新店情報", "閉店情報", "飲食店情報", "ご紹介します", "まとめ",
    "話題の新店", "注目の新店", "新店グルメ", "新店居酒屋",
    "こちら店内", "オープンおめでとう", "本日オープン", "明日オープン",
    "に新店", "新店居酒屋", "新店ラーメン", "新店カフェ", "新店グルメ",
    "待望の新エリア", "ついにオープン", "ご挨拶", "ニューオープン",
    "オープン情報", "開店情報", "新店紹介", "新店舗情報",
    "募集職種", "店舗概要", "会社概要", "求人情報", "採用情報",
    "いよいよ本日", "いよいよ明日", "駅に立ち飲み居酒屋", "駅に居酒屋",
    "に誕生！", "に誕生!", "オープニングスタッフ", "スタッフ募集",
    "求人募集", "募集要項", "採用ページ", "新店舗スタッフ",
]

MULTI_POST_PATTERNS = [
    r"[①②③④⑤⑥⑦⑧⑨⑩]",
    r"(?:^|\s)\d{1,2}[\.．、:：]\s*",
    r"新店情報.*閉店情報",
    r"(?:新オープン|新店).*(?:多数|その[①②③④⑤⑥⑦⑧⑨⑩一二三四五六七八九])",
]


def is_multi_store_post(text: str) -> bool:
    t = clean(text)
    circled = len(re.findall(r"[①②③④⑤⑥⑦⑧⑨⑩]", t))
    if circled >= 2:
        return True
    return any(re.search(p, t, flags=re.I | re.S) for p in MULTI_POST_PATTERNS[2:])


def sanitize_store_candidate(candidate: str) -> str:
    c = clean(candidate)
    c = re.sub(r"^[#＃@＠・\s:：\-—–|｜【】「」『』]+", "", c)
    c = re.sub(r"[#＃\s:：\-—–|｜【】「」『』]+$", "", c)
    c = re.sub(r"^(?:店名|店舗名|名称)\s*[:：]\s*", "", c)
    c = re.sub(r"^(?:札幌市|札幌|千歳市|恵庭市|北広島市|苫小牧市)\s*", "", c)
    c = re.sub(r"^(?:中央区|北区|東区|白石区|豊平区|南区|西区|厚別区|手稲区|清田区)\s*", "", c)

    n = norm(c)
    if not n or n in {norm(x) for x in BAD_STORE_NAMES}:
        return ""
    if any(norm(x) in n for x in BAD_STORE_FRAGMENTS):
        return ""
    if re.fullmatch(r"(?:20\d{2}年)?\d{1,2}月(?:\d{1,2}日)?", c):
        return ""
    if re.fullmatch(r"20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?", c):
        return ""
    if len(c) > 70:
        return ""
    if re.match(r"^(?:いよいよ)?(?:本日|今日|明日|昨日|先月|今月|来月)?\s*\d{1,2}月\d{1,2}日", c):
        return ""
    if re.search(r"(?:募集職種|店舗概要|会社概要|採用情報|求人情報)", c):
        return ""
    if re.search(r"[￥¥]\s*\d|徒歩\d|営業時間|定休日|飲み放題|メニュー|menu", c, flags=re.I):
        return ""
    return c


def extract_store_name(title: str, snippet: str = "") -> str:
    """店名らしい明示表現を優先。説明文や日付を店名にしない。"""
    t = clean(title)
    body = clean(f"{title} {snippet}")

    # 求人・SNS本文の冒頭にある固有店名を優先（「募集職種」等の見出し誤抽出を防止）。
    for p in [
        r"^✨?\s*([^｜|]{2,50}?店)\s*[｜|]",
        r"^([^。\n]{2,50}?店)\s+\d{1,2}月\d{1,2}日(?:の)?(?:グランド)?オープン",
        r"^([^。\n]{2,50}?)(?=\s*(?:オープニングスタッフ募集|スタッフ募集))",
    ]:
        m = re.search(p, body, flags=re.I)
        if m:
            c = sanitize_store_candidate(m.group(1))
            if c:
                return c

    # 「店名：...」「店舗名：...」の明示表現
    for p in [
        r"(?:店名|店舗名|名称)\s*[:：]\s*([^。,\n|｜]{2,60})",
        r"(?:🏠|🍴|☕|🍜)\s*([^📍。\n|｜]{2,60})",
    ]:
        m = re.search(p, body, flags=re.I)
        if m:
            c = sanitize_store_candidate(m.group(1))
            if c:
                return c

    # 【#店名】はSNS紹介投稿で比較的強い。
    for p in [
        r"【\s*[#＃]\s*([^】]{2,60})】",
        r"【\s*([^】]{2,60})】",
        r"「([^」]{2,60})」",
        r"『([^』]{2,60})』",
        r"[\[［]\s*([^\]］]{2,60})[\]］]",
    ]:
        for m in re.finditer(p, body):
            c = sanitize_store_candidate(m.group(1))
            if c and not any(k in norm(c) for k in ["point", "menu", "場所"]):
                return c

    # SNSハッシュタグから固有店名を拾う。地域名・料理ジャンル等は除外する。
    generic_tags = {norm(x) for x in [
        "札幌", "札幌グルメ", "札幌カフェ", "札幌ラーメン", "札幌居酒屋",
        "すすきの", "すすきのグルメ", "円山", "大通", "新店", "新規オープン",
        "開店準備中", "イタリアン", "カフェ", "ラーメン", "居酒屋", "グルメ",
        "苫小牧", "千歳", "恵庭", "北広島", "PR", "北海道",
        "クレープ屋さん", "お店始めます", "開業準備",
    ]}
    tags = re.findall(r"[#＃]([0-9A-Za-zぁ-んァ-ヶ一-龠ー・&'’._-]{2,40})", body)
    for tag in tags:
        c = sanitize_store_candidate(tag)
        if c and norm(c) not in generic_tags and not any(norm(g) in norm(c) for g in ["グルメ", "カフェ巡り", "新店"]):
            return c

    # 「この度、中国料理布袋は...」「新店舗[店名]をオープン」のような本文型
    for p in [
        r"この度[、,\s]*([^。]{2,40}?)(?:は|が)(?=20\d{2}年|\d{1,2}月|札幌市|千歳市|恵庭市|北広島市|苫小牧市)",
        r"新店舗\s*[\[［]([^\]］]{2,50})[\]］]",
    ]:
        m = re.search(p, body, flags=re.I)
        if m:
            c = sanitize_store_candidate(m.group(1))
            if c:
                return c

    # 「焼鳥どん札幌すすきの店 9/28...グランドオープン」のような形
    m = re.search(
        r"^(.{2,60}?)(?=\s+(?:20\d{2}[年/-]|\d{1,2}[/-]\d{1,2}|"
        r"\d{1,2}月\d{1,2}日|プレオープン|グランドオープン|オープン予定|"
        r"新規オープン|NEWOPEN|OPEN|閉店予定|閉店))",
        t, flags=re.I
    )
    if m:
        c = sanitize_store_candidate(m.group(1))
        if c:
            return c

    # 「○○がオープン」「○○は閉店」
    m = re.search(
        r"(.{2,60}?)(?:が|は)\s*(?:\d{1,2}月\d{1,2}日)?\s*"
        r"(?:新規)?(?:オープン予定|開店予定|閉店予定|グランドオープン|"
        r"オープン|OPEN|開店|閉店)",
        t, flags=re.I
    )
    if m:
        c = sanitize_store_candidate(m.group(1))
        if c:
            return c

    # PR等の「『店名』NEWOPEN」は上の引用符で拾う。
    # 最後の救済は、開閉店語より前の短い部分だけ。
    parts = re.split(
        r"(?:が|は|、|に)?\s*(?:新規)?(?:オープン予定|開店予定|閉店予定|"
        r"グランドオープン|オープン|OPEN|開店|閉店|出店予定|出店)",
        t, maxsplit=1, flags=re.I
    )
    c = sanitize_store_candidate(parts[0] if parts else "")
    return c


def refine_store_name(name: str) -> str:
    """Ver.7.3.6: 店名末尾に混入した記事語・新店舗語を軽く除去する。"""
    c = sanitize_store_candidate(name)
    if not c:
        return ""
    c = re.sub(r"(?:の)?新店舗$", "", c).strip()
    c = re.sub(r"(?:の)?新店$", "", c).strip()
    c = re.sub(r"(?:店)?オープニングスタッフ(?:募集)?$", "", c, flags=re.I).strip()
    c = re.sub(r"(?:スタッフ|アルバイト|正社員)募集$", "", c).strip()
    return sanitize_store_candidate(c)


def is_valid_store_name(name: str) -> bool:
    return bool(sanitize_store_candidate(name))


def is_store_name_pending(text: str) -> bool:
    t = norm(text)
    return any(norm(x) in t for x in [
        "店名は近日発表", "店名近日発表", "店名は後日", "店名未定",
        "店名未発表", "店名、お知らせ", "店名をお知らせ",
    ])


def has_unnamed_opening_details(text: str, area: str, status: str, address_hint: str) -> bool:
    """店名未発表でも、場所＋業態＋開店予定が揃う情報は保存対象にする。"""
    if status != "upcoming" or not area or area == "札幌市・区不明":
        return False
    if not is_store_name_pending(text):
        return False
    if not address_hint:
        return False
    return is_food_signal(text)


NON_FOOD_WORDS = [
    "workman", "ワークマン", "美容室", "美容院", "サロン", "ネイル", "整体", "整骨院",
    "クリニック", "歯科", "薬局", "ドラッグストア", "アパレル", "衣料", "古着", "雑貨",
    "不動産", "ホテル", "旅館", "学習塾", "ジム", "フィットネス", "自動車", "中古車",
]

EVENT_WORDS = [
    "期間限定", "限定営業", "ポップアップ", "popup", "催事", "イベント出店", "出店イベント",
    "キッチンカー", "マルシェ", "フェス", "2日間限定", "二日間限定", "1日限定", "一日限定",
    "プチレストラン", "コラボイベント",
]

GENERIC_NAME_WORDS = [
    "ご挨拶", "ニューオープン", "newopen", "新店情報", "新店舗情報", "新店紹介",
    "オープン情報", "開店情報", "閉店情報", "お知らせ", "札幌新店", "すすきの新店",
]


def has_non_food_primary_signal(text: str) -> bool:
    """非飲食業態が主題の候補を公開対象から落とす。"""
    t = norm(text)
    return any(norm(x) in t for x in NON_FOOD_WORDS)


def is_temporary_event(text: str) -> bool:
    t = norm(text)
    return any(norm(x) in t for x in EVENT_WORDS)


def store_name_quality(name: str) -> int:
    """店名らしさを0-3で評価。一般語・文章断片をA判定へ通しにくくする。"""
    c = sanitize_store_candidate(name)
    if not c:
        return 0
    n = norm(c)
    if any(norm(x) == n or norm(x) in n for x in GENERIC_NAME_WORDS):
        return 0
    if len(c) < 2 or len(c) > 50:
        return 0
    if re.search(r"(?:です|ます|ました|します|しました|ください|について|のお知らせ)$", c):
        return 0
    if re.search(r"(?:オープン|OPEN|開店|閉店|出店)(?:予定)?$", c, flags=re.I):
        return 1
    if re.search(r"(?:店|亭|屋|庵|堂|館|軒|家|房|バル|BAR|Cafe|CAFE|カフェ|食堂|酒場|キッチン|ベーカリー|レストラン|ラーメン|珈琲|寿司|鮨)", c, flags=re.I):
        return 3
    if re.search(r"[A-Za-zぁ-んァ-ヶ一-龠]", c) and 2 <= len(c) <= 35:
        return 2
    return 1


def status_near_store(text: str, store_name: str, fallback: str) -> str:
    """店名周辺の文を優先して開閉店状態を判定し、別店舗の閉店語の影響を抑える。"""
    raw = clean(text)
    if not store_name or store_name == "店名未発表":
        return fallback
    pos = norm(raw).find(norm(store_name))
    if pos < 0:
        return fallback
    # norm後の位置は原文と厳密一致しないため、原文でも簡易検索する。
    p2 = raw.casefold().find(store_name.casefold())
    if p2 < 0:
        return fallback
    window = raw[max(0, p2 - 70): p2 + len(store_name) + 130]
    w = norm(window)
    close = any(norm(x) in w for x in CLOSE_WORDS)
    opening = any(norm(x) in w for x in OPEN_WORDS)
    if opening and not close:
        return "upcoming"
    if close and not opening:
        return "closed"
    # 両方ある時は店名の直後に近い語を優先。
    name_end = window.casefold().find(store_name.casefold()) + len(store_name)
    tail = window[name_end:name_end + 90]
    tw = norm(tail)
    if any(norm(x) in tw for x in OPEN_WORDS):
        return "upcoming"
    if any(norm(x) in tw for x in CLOSE_WORDS):
        return "closed"
    return fallback


def date_relation(date_hint: str) -> str:
    """明示日付を future / recent / past / unknown に分類。"""
    if not date_hint:
        return "unknown"
    try:
        parts = date_hint.split("-")
        y, m = int(parts[0]), int(parts[1])
        d = int(parts[2]) if len(parts) >= 3 else 1
        dt = datetime(y, m, d)
    except (ValueError, IndexError):
        return "unknown"
    days = (dt.date() - datetime.now().date()).days
    if days >= 0:
        return "future"
    if days >= -45:
        return "recent"
    return "past"

def closed_within_30_days(date_hint: str) -> bool:
    """閉店情報をA表示できるのは、未来の閉店予定または閉店後30日以内だけ。"""
    if not date_hint:
        return False
    try:
        y, m, d = map(int, date_hint.split("-")[:3])
        dt = datetime(y, m, d).date()
    except (ValueError, IndexError):
        return False
    days = (dt - datetime.now().date()).days
    return days >= -30


def extract_date_hint(text: str) -> str:
    """年なし日付は文脈を見て解釈。過去形のOPENを翌年の予定日にしない。"""
    t = unicodedata.normalize("NFKC", text or "")
    m = re.search(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})日?", t)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(20\d{2})年(\d{1,2})月", t)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.search(r"(\d{1,2})月(\d{1,2})日", t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        now = datetime.now()
        year = now.year
        try:
            candidate = datetime(year, month, day)
        except ValueError:
            return ""

        # 「オープンした」「OPENされた」「先月/○月にOPENした」は過去記事。翌年へ送らない。
        past_context = bool(re.search(
            r"(?:オープン|open|開店)(?:した|しました|された|済み|してから|したばかり)|"
            r"(?:先月|先日|昨日|本日|今日|\d{1,2}月に)\s*(?:オープン|open|開店)(?:した|された|しました)?",
            t, flags=re.I
        ))
        future_context = bool(re.search(
            r"(?:オープン予定|開店予定|グランドオープンに向け|オープンします|開店します|近日オープン|open予定)",
            t, flags=re.I
        ))
        # 「予定」というだけでは翌年へ送らない。
        # 年なし日付を翌年扱いするのは、本文に翌年を示す明確な表現がある場合だけ。
        next_year_context = bool(re.search(
            r"(?:来年|翌年|来春|来夏|来秋|来冬|年明け|次の年)",
            t, flags=re.I
        ))
        if past_context:
            if candidate.date() > now.date() and (candidate - now).days > 30:
                year -= 1
        elif future_context and next_year_context:
            if candidate.date() < now.date():
                year += 1
        # 文脈不明、または単なる「オープン予定」なら勝手に翌年へ送らず当年として保持する。
        return f"{year:04d}-{month:02d}-{day:02d}"
    return ""

def extract_address_hint(text: str) -> str:
    t = clean(text)
    m = re.search(
        r"((?:札幌市)?(?:中央区|北区|東区|白石区|豊平区|南区|西区|厚別区|手稲区|清田区)"
        r"[^。,\n]{0,45}|(?:千歳市|恵庭市|北広島市|苫小牧市)[^。,\n]{0,45})",
        t
    )
    return clean(m.group(1)) if m else ""



def parse_published_date(value: str):
    if not value:
        return None
    for fmt in [
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%d",
    ]:
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            pass
    return None


def signal_is_current(status: str, date_hint: str, published: str = "") -> bool:
    """古い閉店/開店済み投稿を「未確認新着」へ出しにくくする。"""
    now = datetime.now()
    pub = parse_published_date(published)
    if pub and (now - pub).days > 550:
        return False

    if date_hint:
        try:
            parts = date_hint.split("-")
            y, m = int(parts[0]), int(parts[1])
            d = int(parts[2]) if len(parts) >= 3 else 1
            hinted = datetime(y, m, d)
            # 明示日付が1年以上前なら公開候補から除外。
            if (now - hinted).days > 365:
                return False
        except (ValueError, IndexError):
            pass
    return True

def make_queries():
    # 検索数を抑えつつ、SNS・求人・地域情報を横断する。
    area_groups = [
        "札幌",
        "千歳 OR 恵庭",
        "北広島 OR 苫小牧",
    ]
    intents = [
        '飲食店 ("オープン予定" OR "開店予定" OR "新店" OR "閉店予定")',
        '飲食店 ("オープニングスタッフ" OR "新規オープン" OR "新店スタッフ")',
        '(カフェ OR ラーメン OR 居酒屋 OR レストラン OR 焼肉) ("オープン" OR "閉店")',
    ]
    social = [
        '(site:instagram.com OR site:x.com OR site:tiktok.com)',
        '',
    ]
    seen = set()
    for area in area_groups:
        for intent in intents:
            for social_part in social:
                q = f'{area} {intent} {social_part}'.strip()
                if q not in seen:
                    seen.add(q)
                    yield q


def load_confirmed_items(news: dict):
    rows = []
    areas = news.get("areas", {})
    if isinstance(areas, dict):
        for area_key, items in areas.items():
            if not isinstance(items, list):
                continue
            for x in items:
                if not isinstance(x, dict):
                    continue
                rows.append({
                    "name": clean(str(x.get("name") or x.get("title") or "")),
                    "area": clean(str(x.get("ward") or x.get("area") or area_key or "")),
                    "place": clean(str(x.get("place") or x.get("address") or "")),
                    "status": clean(str(x.get("status") or x.get("type") or "")),
                    "url": clean(str(x.get("url") or "")),
                })
    return rows


def name_similarity(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) >= 3 and len(b) >= 3 and (a in b or b in a):
        return 0.92
    return SequenceMatcher(None, a, b).ratio()


def match_confirmed(sig: Signal, confirmed: list[dict]):
    if sig.candidate_type == "unnamed_opening" or sig.store_name == "店名未発表":
        return None, 0.0
    best = None
    best_score = 0.0
    for row in confirmed:
        ns = name_similarity(sig.store_name, row["name"])
        if ns < 0.45:
            continue
        score = ns * 0.72
        if sig.area and sig.area != "札幌市・区不明":
            if sig.area == row["area"] or norm(sig.area) in norm(row["area"]):
                score += 0.18
            elif row["area"]:
                score -= 0.08
        if sig.address_hint and row["place"]:
            if norm(sig.address_hint) in norm(row["place"]) or norm(row["place"]) in norm(sig.address_hint):
                score += 0.10
        if sig.status and row["status"] and sig.status == row["status"]:
            score += 0.05
        if score > best_score:
            best_score = score
            best = row
    return best, round(max(0.0, min(1.0, best_score)), 3)


def signal_confidence(sig: Signal) -> float:
    score = 0.28
    if sig.area and sig.area != "札幌市・区不明":
        score += 0.16
    if sig.store_name and len(norm(sig.store_name)) >= 3:
        score += 0.16
    if sig.status != "unknown":
        score += 0.12
    if sig.date_hint:
        score += 0.08
    if sig.address_hint:
        score += 0.08
    if sig.source_type == "job":
        score += 0.06
    if sig.source_type == "press_release":
        score += 0.10
    if sig.source_type in ("instagram", "x", "tiktok"):
        score += 0.04
    if sig.candidate_type == "unnamed_opening":
        score -= 0.06
        if sig.address_hint:
            score += 0.08
    elif not is_valid_store_name(sig.store_name):
        score -= 0.30
    return round(max(0.0, min(0.95, score)), 2)


def dedupe_signals(signals: list[Signal]):
    merged = {}
    for s in signals:
        if s.candidate_type == "unnamed_opening":
            key = ("unnamed", s.area, norm(s.address_hint) or norm(s.title))
        else:
            key = (norm(s.store_name), s.area, s.status)
            if not key[0]:
                key = (norm(s.title), s.area, s.status)
        if key not in merged:
            merged[key] = s
            continue
        old = merged[key]
        # より情報量の多い方を主レコードにする
        if len(s.snippet) + len(s.address_hint) > len(old.snippet) + len(old.address_hint):
            s.reasons = list(dict.fromkeys(old.reasons + s.reasons))
            merged[key] = s
        else:
            old.reasons = list(dict.fromkeys(old.reasons + s.reasons))
            old.confidence = max(old.confidence, s.confidence)
    return list(merged.values())


def collect_deep_signals():
    today = datetime.now().strftime("%Y-%m-%d")
    results = []
    seen_urls = set()
    query_count = 0

    for query in make_queries():
        query_count += 1
        log.info("深掘り検索: %s", query)
        for row in google_news_rss(query):
            url = row["url"]
            title = row["title"]
            snippet = row["snippet"]
            text = f"{title} {snippet} {row['source']}"

            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            area = detect_area(text)
            if area in {"__excluded__", "__outside__"}:
                continue
            if not area:
                continue
            if not is_food_signal(text):
                continue

            status = detect_status(text)
            if status == "unknown":
                continue

            stype = source_type(url, text, row["source"])
            store = refine_store_name(extract_store_name(title, snippet))
            date_hint = extract_date_hint(text)
            address_hint = extract_address_hint(text)
            status = status_near_store(text, store, status)

            # まとめ投稿は1店舗として誤認しやすいため除外。
            if is_multi_store_post(text):
                log.info("まとめ投稿を除外: %s", title[:100])
                continue

            candidate_type = "named_store"
            # 投稿自身が「店名は近日発表/未発表」と明示している場合は、
            # ハッシュタグの業態語を店名と誤認せず匿名開店候補として扱う。
            if has_unnamed_opening_details(text, area, status, address_hint):
                store = "店名未発表"
                candidate_type = "unnamed_opening"
                log.info("店名未発表の開店予定として保持: %s", title[:100])
            elif not is_valid_store_name(store):
                log.info("店名特定不可を除外: %s", title[:100])
                continue

            if not signal_is_current(status, date_hint, row.get("published", "")):
                log.info("古いシグナルを除外: %s", title[:100])
                continue

            reasons = ["公開Web検索で開店・閉店シグナルを検出"]
            if stype == "job":
                reasons.append("求人・オープニングスタッフ情報")
            if stype == "press_release":
                reasons.append("プレスリリース由来")
            if stype in ("instagram", "x", "tiktok"):
                reasons.append("SNS由来の公開情報/検索インデックス")
            if any(norm(x) in norm(text) for x in RUMOR_WORDS):
                reasons.append("予定・募集等の事前シグナル")
            if candidate_type == "unnamed_opening":
                reasons.append("店名未発表だが地域・業態・開店予定を確認")

            sig = Signal(
                title=title,
                url=url,
                source=row["source"],
                source_type=stype,
                area=area,
                status=status,
                store_name=store,
                address_hint=address_hint,
                date_hint=date_hint,
                snippet=snippet[:500],
                first_seen=today,
                candidate_type=candidate_type,
                reasons=reasons,
            )
            sig.confidence = signal_confidence(sig)
            results.append(sig)

        time.sleep(INTERVAL)

    return dedupe_signals(results), query_count


def has_opened_past_context(text: str) -> bool:
    t = unicodedata.normalize("NFKC", text or "")
    return bool(re.search(
        r"(?:オープン|open|開店)(?:した|しました|された|済み|してから|したばかり)|"
        r"(?:先月|先日|昨日|\d{1,2}月に)\s*(?:オープン|open|開店)(?:した|された|しました)?",
        t, flags=re.I
    ))

def has_strong_future_opening_context(text: str) -> bool:
    t = unicodedata.normalize("NFKC", text or "")
    return bool(re.search(
        r"(?:オープン予定|開店予定|出店予定|開業予定|近日オープン|近日OPEN|"
        r"オープンします|開店します|グランドオープン予定|オープニングスタッフ)",
        t, flags=re.I
    ))


def has_strong_past_opening_context(text: str) -> bool:
    t = unicodedata.normalize("NFKC", text or "")
    return bool(re.search(
        r"(?:オープン|OPEN|開店)(?:した|しました|された|済み|している|しています|したばかり)|"
        r"(?:すでに|既に|先月|先日|昨日|本日|今日)\s*(?:オープン|OPEN|開店)|"
        r"\d{1,2}月\d{1,2}日に?\s*(?:オープン|OPEN|開店)(?:した|しました|された)",
        t, flags=re.I
    ))


def review_signal(sig: Signal) -> tuple[str, str, int, list[str]]:
    """Ver.7.3.6: 公開価値を重視して A/B/C を判定する。"""
    score = 0
    reasons = []
    text = f"{sig.title} {sig.snippet}"

    named = sig.candidate_type == "named_store" and is_valid_store_name(sig.store_name)
    unnamed = sig.candidate_type == "unnamed_opening" and sig.store_name == "店名未発表"
    specific_area = bool(sig.area and sig.area != "札幌市・区不明")
    name_q = store_name_quality(sig.store_name) if named else (1 if unnamed else 0)
    relation = date_relation(sig.date_hint)
    non_food = has_non_food_primary_signal(text)
    temporary = is_temporary_event(text)
    outside = not has_target_location_context(text)
    geo_ok = target_geo_consistent(text, sig.area)
    opened_past = has_opened_past_context(text) or has_strong_past_opening_context(text)
    strong_future = has_strong_future_opening_context(text)

    if named:
        score += 2 + name_q
        reasons.append(f"店名品質:{name_q}")
    elif unnamed:
        score += 1
        reasons.append("店名未発表案件")
    else:
        score -= 4
        reasons.append("店名の信頼性が低い")

    if specific_area:
        score += 2
        reasons.append("対象地域を特定")
    else:
        score -= 3
        reasons.append("区市町村を特定できない")

    if sig.address_hint:
        score += 2
        reasons.append("住所・場所情報あり")
    if sig.date_hint:
        score += 1
        reasons.append("開閉店時期あり")

    if sig.status in ("upcoming", "closed"):
        score += 1
        reasons.append("開閉店状態を判定")

    if sig.source_type == "press_release":
        score += 3
        reasons.append("プレスリリース")
    elif sig.source_type == "job":
        score += 2
        reasons.append("求人・オープニング情報")
    elif sig.source_type in ("instagram", "x", "tiktok"):
        score += 1
        reasons.append("SNS公開情報")

    if sig.confidence >= 0.85:
        score += 2
        reasons.append("抽出信頼度が高い")
    elif sig.confidence >= 0.70:
        score += 1
        reasons.append("抽出信頼度が一定以上")
    elif sig.confidence < 0.60:
        score -= 2
        reasons.append("抽出信頼度が低い")

    if sig.match_score >= 0.55:
        score += 1
        reasons.append("既存情報に類似候補あり")

    # 公開精度を上げる強制条件
    if outside:
        reasons.append("対象地域外の地名を主対象として検出")
        return "C", "保存のみ", min(score, 2), reasons
    if not geo_ok:
        reasons.append("対象地域との市区整合性を確認できない")
        score = min(score, 7)
    if non_food:
        reasons.append("非飲食業態の可能性が高い")
        return "C", "保存のみ", min(score, 3), reasons
    if temporary:
        reasons.append("期間限定イベント・催事の可能性")
        return "C", "保存のみ", min(score, 3), reasons
    if named and name_q == 0:
        reasons.append("一般語・文章断片を店名として検出")
        return "C", "保存のみ", min(score, 3), reasons
    if opened_past and sig.status == "upcoming":
        score -= 5
        reasons.append("本文が開店済みの過去形")

    # upcoming は未来予定を最優先。45日以内の開店済みはBへ残す。
    if sig.status == "upcoming" and relation == "past":
        score -= 4
        reasons.append("開店予定日が既に45日超過")
    elif sig.status == "upcoming" and relation == "recent":
        score -= 2
        reasons.append("直近開店済みの可能性")
    elif sig.status == "upcoming" and relation == "future":
        score += 2
        reasons.append("将来の開店予定日")

    # 閉店は未来予定または閉店後30日以内だけA候補。31日以上前はB以下へ。
    closed_current = sig.status == "closed" and closed_within_30_days(sig.date_hint)
    if sig.status == "closed" and sig.date_hint and not closed_current:
        score -= 4
        reasons.append("閉店後30日を超過")
    elif sig.status == "closed" and closed_current:
        score += 1
        reasons.append("閉店予定または閉店後30日以内")

    # 店名未発表は住所＋将来予定が揃う場合だけAへ。
    if unnamed:
        if not (sig.address_hint and sig.status == "upcoming" and relation == "future"):
            score = min(score, 7)
            reasons.append("店名未発表のため追加確認が必要")

    # Aは「店名品質＋地域＋現在性」を要求。日付なしは一次性の高い媒体等が必要。
    a_current = (not opened_past) and (
        relation == "future"
        or closed_current
        or (not sig.date_hint and sig.source_type in ("press_release", "job") and strong_future)
    )
    a_name = (named and name_q >= 2) or unnamed
    if specific_area and geo_ok and a_name and a_current and score >= 10:
        return "A", "掲載候補", score, reasons
    if specific_area and (named or unnamed) and score >= 5:
        return "B", "要確認", score, reasons
    return "C", "保存のみ", score, reasons

def classify_signals(signals: list[Signal], confirmed: list[dict]):
    unconfirmed_all = []
    matched = []

    for sig in signals:
        row, score = match_confirmed(sig, confirmed)
        sig.match_score = score

        if row and score >= 0.78:
            sig.verification = "matched_confirmed"
            sig.matched_store = row["name"]
            sig.matched_url = row["url"]
            sig.reasons.append("既存の確定情報と高一致")
            sig.review_class = "C"
            sig.review_label = "既存一致"
            sig.review_score = 0
            sig.review_reasons = ["既存確定情報と高一致のため未確認欄へ表示しない"]
            matched.append(sig)
            continue

        sig.verification = "unconfirmed"
        if row and score >= 0.55:
            sig.matched_store = row["name"]
            sig.matched_url = row["url"]
            sig.reasons.append("既存情報に類似候補あり・要確認")
        else:
            sig.reasons.append("既存の確定情報に十分な一致なし")

        cls, label, review_score, review_reasons = review_signal(sig)
        sig.review_class = cls
        sig.review_label = label
        sig.review_score = review_score
        sig.review_reasons = review_reasons
        sig.display_eligible = cls == "A"
        unconfirmed_all.append(sig)

    unconfirmed_all.sort(key=lambda x: (x.review_class, -x.review_score, -x.confidence, x.area, x.store_name))
    matched.sort(key=lambda x: (-x.match_score, x.area, x.store_name))
    return matched, unconfirmed_all

def write_outputs(news: dict, signals, matched, unconfirmed, query_count):
    now = datetime.now().isoformat(timespec="seconds")

    social_payload = {
        "version": VERSION,
        "generated_at": now,
        "description": "公開Web・SNS検索インデックス・求人・プレスリリース等から得た深掘り候補。確定情報ではありません。",
        "count": len(signals),
        "items": [asdict(x) for x in signals],
    }
    SOCIAL_PATH.write_text(
        json.dumps(social_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    class_counts = {"A": 0, "B": 0, "C": 0}
    for x in unconfirmed:
        class_counts[x.review_class] = class_counts.get(x.review_class, 0) + 1
    display_items = [x for x in unconfirmed if x.review_class == "A"]

    unconfirmed_payload = {
        "version": VERSION,
        "generated_at": now,
        "description": "未確認候補をA=掲載候補、B=要確認、C=保存のみの3段階で保存。確定情報ではありません。",
        "count": len(unconfirmed),
        "display_count": len(display_items),
        "class_counts": class_counts,
        "items": [asdict(x) for x in unconfirmed],
    }
    UNCONFIRMED_PATH.write_text(
        json.dumps(unconfirmed_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # index.html側が将来そのまま使えるよう news.json にも別枠で付与。
    # 既存 areas/events は変更しない。
    news["collector_version"] = VERSION
    news["unconfirmed_count"] = len(display_items)
    news["unconfirmed_total_count"] = len(unconfirmed)
    news["unconfirmed_class_counts"] = class_counts
    news["unconfirmed_updated_at"] = now
    news["unconfirmed"] = [asdict(x) for x in display_items]
    NEWS_PATH.write_text(
        json.dumps(news, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report = {
        "version": VERSION,
        "generated_at": now,
        "queries": query_count,
        "deep_signals": len(signals),
        "matched_confirmed": len(matched),
        "unconfirmed_total": len(unconfirmed),
        "unconfirmed_display": len(display_items),
        "review_class_counts": class_counts,
        "target_areas": TARGET_AREAS,
        "excluded_areas": EXCLUDED_AREAS,
        "source_type_counts": {},
        "area_counts": {},
    }
    for x in signals:
        report["source_type_counts"][x.source_type] = report["source_type_counts"].get(x.source_type, 0) + 1
        report["area_counts"][x.area] = report["area_counts"].get(x.area, 0) + 1

    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def main():
    log.info("===== Sapporo Inshokuten Collector Ver.%s START =====", VERSION)

    # 1) 現行Ver.7.2をそのまま実行。確定情報ラインを壊さない。
    core = load_core()
    log.info("=== Ver.7.2 確定情報ライン実行 ===")
    core.main()

    if not NEWS_PATH.exists():
        raise FileNotFoundError("Ver.7.2実行後に news.json が見つかりません。")

    news = json.loads(NEWS_PATH.read_text(encoding="utf-8"))
    confirmed = load_confirmed_items(news)
    log.info("既存確定情報: %d件", len(confirmed))

    # 2) 深掘りライン
    log.info("=== Ver.7.3.6 深掘りシグナル収集 ===")
    signals, query_count = collect_deep_signals()
    log.info("深掘り候補: %d件", len(signals))

    # 3) 店名・住所・地域・状態を照合
    matched, unconfirmed = classify_signals(signals, confirmed)

    # 4) 別枠JSONへ保存。確定DBには入れない。
    report = write_outputs(news, signals, matched, unconfirmed, query_count)

    log.info(
        "深掘り: %d件 / 既存一致: %d件 / 未確認総数: %d件 / 掲載候補A: %d件 / B: %d件 / C: %d件",
        report["deep_signals"],
        report["matched_confirmed"],
        report["unconfirmed_total"],
        report["unconfirmed_display"],
        report["review_class_counts"].get("B", 0),
        report["review_class_counts"].get("C", 0),
    )
    log.info("social_signals.json: %s", SOCIAL_PATH)
    log.info("unconfirmed_signals.json: %s", UNCONFIRMED_PATH)
    log.info("===== Ver.%s END =====", VERSION)


if __name__ == "__main__":
    main()
