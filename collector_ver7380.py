# -*- coding: utf-8 -*-
"""
札幌・道央 飲食店 開店閉店コレクター Ver.7.3.7.3
================================================
Ver.7.2 を確定情報ラインとしてそのまま実行した後、
公開Web上の「SNS痕跡・求人・地域記事・マイナー情報」を検索/RSS経由で深掘りし、
既存 news.json と照合して未確認情報を別ファイルへ分離する追加レイヤー。

重要:
- 確定情報のDBには未確認情報を混ぜない
- 江別市は対象外
- 対象: 札幌10区 + 函館市
- 1情報源が失敗しても全体を止めない
- social_signals.json: 深掘りで取得した全候補
- unconfirmed_signals.json: 既存確定情報と十分一致しない候補
- news.json: Ver.7.2の内容を維持しつつ unconfirmed_* メタ情報を追加

必要:
    collector_ver72_hakodate.py が同じフォルダにあること
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

VERSION = "7.3.8.1"
BASE_DIR = Path(__file__).resolve().parent
CORE_PATH = BASE_DIR / "collector_ver72_hakodate.py"
NEWS_PATH = BASE_DIR / "news.json"
SOCIAL_PATH = BASE_DIR / "social_signals.json"
UNCONFIRMED_PATH = BASE_DIR / "unconfirmed_signals.json"
REPORT_PATH = BASE_DIR / "collector_ver7380_report.json"
LOG_PATH = BASE_DIR / "collector_ver7380.log"

TARGET_AREAS = [
    "中央区", "北区", "東区", "白石区", "豊平区", "南区", "西区",
    "厚別区", "手稲区", "清田区",
    "函館市",
]
EXCLUDED_AREAS = ["江別市", "江別", "千歳市", "恵庭市", "北広島市", "苫小牧市", "北斗市", "七飯町", "新千歳空港"]

AREA_HINTS = {
    "中央区": ["中央区", "すすきの", "大通", "狸小路", "円山", "札幌駅南口"],
    "北区": ["北区", "札幌駅北口", "北24条", "麻生", "新琴似"],
    "東区": ["東区", "苗穂", "元町", "栄町", "東区役所前", "環状通東", "伏古", "丘珠"],
    "白石区": ["白石区", "菊水", "東札幌", "白石", "南郷7丁目", "南郷13丁目", "南郷18丁目", "北郷", "川下"],
    "豊平区": ["豊平区", "平岸", "中の島", "美園", "月寒", "福住", "豊平公園", "学園前"],
    "南区": ["南区", "真駒内", "澄川", "自衛隊前", "石山", "藤野", "川沿", "定山渓", "藻岩"],
    "西区": ["西区", "琴似", "発寒", "二十四軒", "宮の沢", "八軒", "西野", "山の手"],
    "厚別区": ["厚別区", "新札幌", "新さっぽろ", "大谷地", "ひばりが丘", "厚別中央", "厚別西", "もみじ台"],
    "手稲区": ["手稲区", "手稲", "星置", "稲穂", "前田", "新発寒", "富丘"],
    "清田区": ["清田区", "清田", "平岡", "美しが丘", "里塚", "北野", "真栄"],
    "函館市": ["函館市", "函館駅", "五稜郭", "湯の川", "本町", "美原", "函館朝市"],
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
log = logging.getLogger("ver7373")
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
        raise FileNotFoundError("collector_ver72_hakodate.py が同じフォルダにありません。")
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
        if not re.search(r"(?:北海道)?(?:札幌市|函館市)", t):
            return False
        # 見出し冒頭の【江東区】等は記事の主対象地域として優先。
        if re.search(r"^【?(?:江東区|浜松市|東京都|大阪市|名古屋市|福岡市|神戸市|横浜市|さいたま市)", t):
            return False
    return True


# Ver.7.3.8.1: 原材料・食材の産地を店舗所在地と誤認しない。
# 例: 「千歳市戸田牧場の生乳100%使用」は千歳市の店舗を意味しない。
def mask_ingredient_origin_context(text: str) -> str:
    t = clean(text)
    place = r"(?:札幌市|函館市)"
    origin_words = (
        r"(?:産|産地|原料|原材料|食材|生乳|牛乳|乳製品|小麦|米|野菜|肉|魚|"
        r"使用|使用した|使用の|仕入れ|仕入|直送|牧場|農場|農園)"
    )
    # 地名から40文字以内に産地・原材料語が続く場合、その地名だけをマスクする。
    return re.sub(
        rf"({place})(?=[^。！？\n]{{0,40}}{origin_words})",
        "__ORIGIN_PLACE__",
        t,
        flags=re.I,
    )


def has_relative_past_opening_context(text: str) -> bool:
    """「昨年10月OPEN」等を将来予定として扱わない。"""
    t = unicodedata.normalize("NFKC", text or "")
    return bool(re.search(
        r"(?:昨年|去年|前年)\s*\d{1,2}月(?:\d{1,2}日)?[^。！？\n]{0,24}"
        r"(?:オープン|OPEN|開店)",
        t,
        flags=re.I,
    ))


def store_location_status_same_context(text: str, store_name: str, area: str) -> bool:
    """A掲載用: 店名・所在地根拠・OPEN/CLOSEが近い文脈にあることを確認。"""
    if not store_name or not area:
        return False
    t = mask_ingredient_origin_context(text)
    nt = norm(t)
    ns = norm(store_name)
    if not ns or ns not in nt:
        return False

    status_pat = re.compile(
        r"(?:オープン|OPEN|開店|閉店|営業終了|グランドオープン)",
        flags=re.I,
    )
    for m in status_pat.finditer(t):
        lo = max(0, m.start() - 140)
        hi = min(len(t), m.end() + 140)
        window = t[lo:hi]
        if norm(store_name) not in norm(window):
            continue
        if area in {"中央区","北区","東区","白石区","豊平区","南区","西区","厚別区","手稲区","清田区"}:
            if re.search(rf"(?:北海道)?札幌市\s*{re.escape(area)}", window):
                return True
            hints = [h for h in AREA_HINTS.get(area, []) if h != area]
            if ("札幌" in window or "北海道" in window) and any(h in window for h in hints):
                return True
        else:
            if area in window:
                return True
    return False


def target_geo_consistent(text: str, area: str) -> bool:
    """Ver.7.3.6: A掲載前の厳格な地域整合性確認。全国にある区名単独を札幌扱いしない。"""
    t = mask_ingredient_origin_context(text)
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
    t0 = mask_ingredient_origin_context(text)
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



# Ver.7.3.7.3: 「札幌市・区不明」を再判定するための区固有地名。
# 複数区にまたがりやすい一般地名は極力入れず、固有性の高い語を優先する。
WARD_REFINEMENT_HINTS = {
    "中央区": ["西11丁目", "西18丁目", "バスセンター前", "桑園", "中島公園", "山鼻", "宮の森"],
    "北区": ["北12条", "北18条", "北34条", "篠路", "屯田", "拓北", "あいの里"],
    "東区": ["東苗穂", "本町", "北光", "北丘珠", "東雁来", "札幌村"],
    "白石区": ["本郷通", "本通", "栄通", "流通センター", "米里", "川北"],
    "豊平区": ["月寒中央", "月寒東", "豊平", "西岡", "羊ケ丘", "旭町"],
    "南区": ["真駒内", "澄川", "石山", "藤野", "川沿", "定山渓", "常盤", "簾舞", "南沢"],
    "西区": ["琴似", "発寒", "二十四軒", "宮の沢", "八軒", "西野", "山の手", "平和"],
    "厚別区": ["新札幌", "新さっぽろ", "大谷地", "ひばりが丘", "厚別中央", "厚別西", "厚別東", "厚別南", "もみじ台", "青葉町"],
    "手稲区": ["手稲本町", "星置", "稲穂", "前田", "新発寒", "富丘", "曙", "金山"],
    "清田区": ["清田", "平岡", "美しが丘", "里塚", "北野", "真栄", "有明"],
}

def refine_sapporo_unknown_area(text: str, current_area: str) -> str:
    """札幌市・区不明だけを、住所表記・区固有地名から安全側に再判定する。"""
    if current_area != "札幌市・区不明":
        return current_area
    t = unicodedata.normalize("NFKC", clean(text))

    # 明示の「札幌市○区」があれば最優先。
    m = re.search(r"(?:北海道)?札幌市\s*(中央区|北区|東区|白石区|豊平区|南区|西区|厚別区|手稲区|清田区)", t)
    if m:
        return m.group(1)

    scores = {}
    for ward, hints in WARD_REFINEMENT_HINTS.items():
        hits = [h for h in hints if h in t]
        if hits:
            scores[ward] = len(hits)

    if not scores:
        return current_area
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    # 同点なら誤判定防止のため区不明のまま。
    if len(ranked) >= 2 and ranked[0][1] == ranked[1][1]:
        return current_area
    return ranked[0][0]


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
    "すすきの", "大通", "狸小路", "円山", "札幌", "札幌市", "函館", "函館市",
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
    c = re.sub(r"^(?:札幌市|札幌|函館市)\s*", "", c)
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
    # Ver.7.3.7.3: 「清田区にラーメン新店！」等の記事説明文を店名にしない。
    if re.match(r"^(?:に|で|から|へ)(?:ラーメン|カフェ|居酒屋|焼肉|飲食|新店|店舗)", c):
        return ""
    if re.search(r"(?:新店|新店舗)[！!？?]|駐車場|人気の定番|を読み解く|出店した理由", c):
        return ""
    if re.search(r"(?:めぐり|巡り)第[0-9０-９一二三四五六七八九十]+弾|^南幌めぐり", c, flags=re.I):
        return ""
    if re.search(r"\s[-–—|｜]\s*(?:SASARU|札幌速報|PR TIMES|号外NET|Yahoo|食べログ)\s*$", c, flags=re.I):
        return ""
    if re.match(r"^(?:いよいよ)?(?:本日|今日|明日|昨日|先月|今月|来月)?\s*\d{1,2}月\d{1,2}日", c):
        return ""
    if re.search(r"(?:募集職種|店舗概要|会社概要|採用情報|求人情報)", c):
        return ""
    if re.search(r"[￥¥]\s*\d|徒歩\d|営業時間|定休日|飲み放題|メニュー|menu", c, flags=re.I):
        return ""
    return c


LOCATION_ONLY_NAMES = {
    "中央区", "北区", "東区", "白石区", "豊平区", "南区", "西区", "厚別区", "手稲区", "清田区",
    "菊水", "菊水駅", "白石", "白石駅", "手稲", "手稲駅", "清田", "月寒", "福住", "琴似", "発寒",
    "新札幌", "新さっぽろ", "函館", "函館駅", "五稜郭",
}

HEADLINE_NOISE_PATTERNS = [
    r"北海道初上陸", r"全国初", r"全国拡大中", r"待望の", r"話題の", r"注目の", r"速報", r"朗報",
    r"ニューオープン", r"新規オープン", r"オープン予定", r"開店予定", r"閉店予定",
]

def is_location_only_store_name(name: str) -> bool:
    n = norm(name)
    if not n:
        return True
    return any(n == norm(x) for x in LOCATION_ONLY_NAMES)

def is_headline_noise_name(name: str) -> bool:
    c = clean(name)
    return any(re.search(p, c, flags=re.I) for p in HEADLINE_NOISE_PATTERNS)

def quoted_store_candidates(text: str) -> list[str]:
    """見出し・本文の引用符内から、飲食店らしい固有名を優先抽出。"""
    out = []
    for p in [r"「([^」]{2,60})」", r"『([^』]{2,60})』", r"[\[［]([^\]］]{2,60})[\]］]"]:
        for m in re.finditer(p, clean(text)):
            c = sanitize_store_candidate(m.group(1))
            if not c or is_location_only_store_name(c) or is_headline_noise_name(c):
                continue
            # 引用内に店名語があるものを最優先。英字ブランド名も許容。
            if store_name_quality(c) >= 2:
                out.append(c)
    return out

def strong_area_evidence(text: str, area: str) -> bool:
    """A/B公開候補用。検索語ヒットだけでなく、記事本文側に対象地域の強い根拠を要求。"""
    t = mask_ingredient_origin_context(text)
    if not area or area == "札幌市・区不明":
        return False
    if area in {"中央区","北区","東区","白石区","豊平区","南区","西区","厚別区","手稲区","清田区"}:
        # 「札幌市○区」または札幌明示＋区名を最強根拠とする。
        if re.search(rf"(?:北海道)?札幌市\s*{re.escape(area)}", t):
            return True
        if "札幌" in t and area in t:
            return True
        # 区固有の地点名が本文にあり、かつ北海道/札幌文脈がある場合。
        unique = [h for h in AREA_HINTS.get(area, []) if h != area]
        if ("札幌" in t or "北海道" in t) and any(h in t for h in unique):
            return True
        return False
    # 近郊4市は市名を本文に要求。駅名等だけで他県を拾わない。
    return area in t

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

    # Ver.7.3.7.3: PR記事等は見出し冒頭の煽り文より「引用符内の実店舗名」を優先。
    quoted = quoted_store_candidates(body)
    if quoted:
        return quoted[0]

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
        "函館", "PR", "北海道",
        "クレープ屋さん", "お店始めます", "開業準備",
    ]}
    tags = re.findall(r"[#＃]([0-9A-Za-zぁ-んァ-ヶ一-龠ー・&'’._-]{2,40})", body)
    for tag in tags:
        c = sanitize_store_candidate(tag)
        if c and norm(c) not in generic_tags and not any(norm(g) in norm(c) for g in ["グルメ", "カフェ巡り", "新店"]):
            return c

    # 「この度、中国料理布袋は...」「新店舗[店名]をオープン」のような本文型
    for p in [
        r"この度[、,\s]*([^。]{2,40}?)(?:は|が)(?=20\d{2}年|\d{1,2}月|札幌市|函館市)",
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
    """Ver.7.3.7.3: 年なし月日を現在年へ安易に補完しない。"""
    t = unicodedata.normalize("NFKC", text or "")
    now = datetime.now()

    era = re.search(r"(令和|平成)(元|\d{1,2})年(\d{1,2})月(\d{1,2})日", t)
    if era:
        era_name, era_year_raw, month, day = era.groups()
        era_year = 1 if era_year_raw == "元" else int(era_year_raw)
        year = (2018 + era_year) if era_name == "令和" else (1988 + era_year)
        try:
            datetime(year, int(month), int(day))
            return f"{year:04d}-{int(month):02d}-{int(day):02d}"
        except ValueError:
            return ""

    m = re.search(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})日?", t)
    if m:
        year, month, day = map(int, m.groups())
        try:
            datetime(year, month, day)
            return f"{year:04d}-{month:02d}-{day:02d}"
        except ValueError:
            return ""

    m = re.search(r"(20\d{2})年(\d{1,2})月", t)
    if m:
        year, month = map(int, m.groups())
        return f"{year:04d}-{month:02d}" if 1 <= month <= 12 else ""

    # Ver.7.3.8.1: 「昨年10月OPEN」等の月単位表現を前年として確定。
    rel_month = re.search(
        r"(昨年|去年|前年|今年|本年|来年|翌年)\s*(\d{1,2})月"
        r"[^。！？\n]{0,24}(?:オープン|OPEN|開店|閉店)",
        t,
        flags=re.I,
    )
    if rel_month:
        rel, month_raw = rel_month.groups()
        month = int(month_raw)
        if 1 <= month <= 12:
            if rel in ("昨年", "去年", "前年"):
                year = now.year - 1
            elif rel in ("来年", "翌年"):
                year = now.year + 1
            else:
                year = now.year
            return f"{year:04d}-{month:02d}"

    m = re.search(
        r"(?<!\d)(\d{1,2})月(\d{1,2})日"
        r"(?:\s*[\(（]\s*([月火水木金土日])(?:曜(?:日)?)?\s*[\)）])?",
        t
    )
    if not m:
        return ""

    month, day = int(m.group(1)), int(m.group(2))
    weekday_jp = m.group(3)
    try:
        datetime(now.year, month, day)
    except ValueError:
        return ""

    year_words = [
        (r"(?:今年|本年|当年)", now.year),
        (r"(?:来年|翌年|次の年)", now.year + 1),
        (r"(?:昨年|去年|前年)", now.year - 1),
    ]
    for pattern, year in year_words:
        if re.search(pattern, t):
            try:
                dt = datetime(year, month, day)
            except ValueError:
                return ""
            if weekday_jp and "月火水木金土日"[dt.weekday()] != weekday_jp:
                return ""
            return f"{year:04d}-{month:02d}-{day:02d}"

    if weekday_jp:
        wd = "月火水木金土日".index(weekday_jp)
        candidates = []
        for year in range(now.year - 2, now.year + 3):
            try:
                dt = datetime(year, month, day)
            except ValueError:
                continue
            if dt.weekday() == wd:
                candidates.append(dt)

        future_context = bool(re.search(
            r"(?:オープン予定|開店予定|閉店予定|オープンします|開店します|閉店します|"
            r"グランドオープン|new\s*open|近日オープン|open予定|予定です)",
            t, flags=re.I
        ))
        past_context = bool(re.search(
            r"(?:オープン|open|開店|閉店)(?:した|しました|された|済み|してから|したばかり)|"
            r"(?:先月|先日|昨日|本日|今日|\d{1,2}月に)\s*(?:オープン|open|開店|閉店)(?:した|された|しました)?",
            t, flags=re.I
        ))

        if future_context and not past_context:
            future = [dt for dt in candidates if dt.date() >= now.date()]
            if future:
                dt = min(future, key=lambda x: x.date())
                return f"{dt.year:04d}-{month:02d}-{day:02d}"

        if past_context and not future_context:
            past = [dt for dt in candidates if dt.date() <= now.date()]
            if past:
                dt = max(past, key=lambda x: x.date())
                return f"{dt.year:04d}-{month:02d}-{day:02d}"

        current = [dt for dt in candidates if dt.year == now.year]
        if current:
            return f"{now.year:04d}-{month:02d}-{day:02d}"

    return ""


def extract_date_hint_enhanced(text: str, published: str = "") -> str:
    """7.3.7.3: 通常抽出で取れない年なし日付/月を、記事公開年を根拠に限定補完する。"""
    base = extract_date_hint(text)
    if base:
        return base

    pub = parse_published_date(published)
    if not pub:
        return ""

    t = unicodedata.normalize("NFKC", text or "")
    # 開閉店の予定・実績語が無い記事からは補完しない。
    if not re.search(
        r"(?:オープン予定|開店予定|閉店予定|グランドオープン|新規オープン|"
        r"オープン|OPEN|開店|閉店|営業終了)", t, flags=re.I
    ):
        return ""

    # 「10月22日オープン」のような年なし月日。
    m = re.search(
        r"(?<!\d)(\d{1,2})月(\d{1,2})日.{0,24}?"
        r"(?:オープン予定|開店予定|閉店予定|グランドオープン|新規オープン|オープン|OPEN|開店|閉店)",
        t, flags=re.I
    )
    if not m:
        m = re.search(
            r"(?:オープン予定|開店予定|閉店予定|グランドオープン|新規オープン|オープン|OPEN|開店|閉店)"
            r".{0,24}?(?<!\d)(\d{1,2})月(\d{1,2})日",
            t, flags=re.I
        )
    if m:
        month, day = map(int, m.groups())
        candidates = []
        for year in (pub.year - 1, pub.year, pub.year + 1):
            try:
                dt = datetime(year, month, day)
            except ValueError:
                continue
            distance = abs((dt.date() - pub.date()).days)
            if distance <= 240:
                candidates.append((distance, dt))
        if candidates:
            dt = min(candidates, key=lambda x: x[0])[1]
            return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"

    # 「10月オープン予定」も公開年との距離が近い場合だけ月単位で保持。
    m = re.search(
        r"(?<!\d)(\d{1,2})月.{0,18}?"
        r"(?:オープン予定|開店予定|閉店予定|グランドオープン|新規オープン|オープン|OPEN|開店|閉店)",
        t, flags=re.I
    )
    if m:
        month = int(m.group(1))
        candidates = []
        for year in (pub.year - 1, pub.year, pub.year + 1):
            try:
                dt = datetime(year, month, 1)
            except ValueError:
                continue
            distance = abs((dt.date() - pub.date()).days)
            if distance <= 240:
                candidates.append((distance, dt))
        if candidates:
            dt = min(candidates, key=lambda x: x[0])[1]
            return f"{dt.year:04d}-{dt.month:02d}"
    return ""


def extract_address_hint(text: str) -> str:
    t = clean(text)
    m = re.search(
        r"((?:札幌市)?(?:中央区|北区|東区|白石区|豊平区|南区|西区|厚別区|手稲区|清田区)"
        r"[^。,\n]{0,45}|函館市[^。,\n]{0,45})",
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
    """Ver.7.3.7.3: 中央区・北区以外を重点深掘りする検索クエリ。"""
    seen = set()

    # 全域の基礎検索。中央区・北区は従来相当の検索量に留める。
    base_groups = ["札幌", "函館市"]
    base_intents = [
        '飲食店 ("オープン予定" OR "開店予定" OR "新店" OR "閉店予定")',
        '飲食店 ("オープニングスタッフ" OR "新規オープン" OR "新店スタッフ")',
        '(カフェ OR ラーメン OR 居酒屋 OR レストラン OR 焼肉) ("オープン" OR "閉店")',
    ]
    for area in base_groups:
        for intent in base_intents:
            for social_part in ['(site:instagram.com OR site:x.com OR site:tiktok.com)', '']:
                q = f'{area} {intent} {social_part}'.strip()
                if q not in seen:
                    seen.add(q)
                    yield q

    # 中央区・北区以外の札幌8区を重点検索。
    focus_areas = {
        "札幌市東区": ["苗穂", "元町", "栄町", "東区役所前", "環状通東", "伏古", "丘珠"],
        "札幌市白石区": ["菊水", "東札幌", "白石", "南郷7丁目", "南郷13丁目", "南郷18丁目", "北郷", "川下"],
        "札幌市豊平区": ["平岸", "中の島", "美園", "月寒", "福住", "豊平公園", "学園前"],
        "札幌市南区": ["真駒内", "澄川", "自衛隊前", "石山", "藤野", "川沿", "定山渓"],
        "札幌市西区": ["琴似", "発寒", "二十四軒", "宮の沢", "八軒", "西野", "山の手"],
        "札幌市厚別区": ["新札幌", "新さっぽろ", "大谷地", "ひばりが丘", "厚別中央", "厚別西", "もみじ台"],
        "札幌市手稲区": ["手稲", "星置", "稲穂", "前田", "新発寒", "富丘"],
        "札幌市清田区": ["清田", "平岡", "美しが丘", "里塚", "北野", "真栄"],
    }
    focus_intents = [
        '(飲食 OR カフェ OR ラーメン OR 居酒屋 OR レストラン) ("オープン" OR "開店" OR "新店" OR "閉店")',
        '("オープニングスタッフ" OR "新規オープン" OR "新店スタッフ" OR "店舗スタッフ募集")',
        '("テナント" OR "出店" OR "新店舗" OR "開業") (飲食 OR カフェ OR レストラン)',
    ]
    for area, spots in focus_areas.items():
        # 区名そのものを重点検索
        for intent in focus_intents:
            q = f'{area} {intent}'
            if q not in seen:
                seen.add(q); yield q
        # 駅名・地域名を束ねて深掘り（クエリ爆発を防ぐため3地点ずつ）
        for i in range(0, len(spots), 3):
            spot_group = " OR ".join(spots[i:i+3])
            for intent in [focus_intents[0], focus_intents[1]]:
                q = f'{area} ({spot_group}) {intent}'
                if q not in seen:
                    seen.add(q); yield q

    # 函館市の駅・繁華街・周辺地区を深掘り。
    nearby = {"函館市": ["函館駅", "五稜郭", "湯の川", "本町", "美原", "函館朝市"]}
    for city, spots in nearby.items():
        for i in range(0, len(spots), 3):
            spot_group = " OR ".join(spots[i:i+3])
            for intent in [focus_intents[0], focus_intents[1], focus_intents[2]]:
                q = f'{city} ({spot_group}) {intent}'
                if q not in seen:
                    seen.add(q); yield q


    # Ver.7.3.7.3: 取得件数が弱かった南区・厚別区・恵庭市だけ追加深掘り。
    weak_area_queries = {
        "札幌市南区": [
            "真駒内 OR 澄川 OR 石山 OR 藤野 OR 川沿 OR 定山渓 OR 常盤 OR 簾舞",
            "南区 求人 オープニング 飲食",
        ],
        "札幌市厚別区": [
            "新札幌 OR 新さっぽろ OR 大谷地 OR ひばりが丘 OR 厚別中央 OR もみじ台",
            "厚別区 求人 オープニング 飲食",
        ],
        "函館市": [
            "函館駅 OR 五稜郭 OR 湯の川 OR 本町 OR 美原",
            "函館 求人 オープニング 飲食",
        ],
    }
    weak_intents = [
        '("新店" OR "オープン予定" OR "開店予定" OR "閉店予定") (飲食 OR カフェ OR ラーメン OR 居酒屋)',
        '("テナント" OR "新店舗" OR "オープニングスタッフ") (飲食 OR カフェ OR レストラン)',
        '(site:instagram.com OR site:x.com OR site:tiktok.com) ("オープン" OR "閉店" OR "新店")',
    ]
    for area, spot_groups in weak_area_queries.items():
        for spot_group in spot_groups:
            for intent in weak_intents:
                q = f'{area} ({spot_group}) {intent}'
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
            area = refine_sapporo_unknown_area(text, area)
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
            date_hint = extract_date_hint_enhanced(text, row.get("published", ""))
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
    """Ver.7.3.8.1: 7.3.7.3基準を維持し、過去年・産地・店名ノイズ・文脈整合だけを補強。"""
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
    strong_geo = strong_area_evidence(text, sig.area)
    location_only_name = named and is_location_only_store_name(sig.store_name)
    headline_noise_name = named and is_headline_noise_name(sig.store_name)
    opened_past = (has_opened_past_context(text) or has_strong_past_opening_context(text) or has_relative_past_opening_context(text))
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
    if named and (name_q == 0 or location_only_name or headline_noise_name):
        if location_only_name:
            reasons.append("駅名・区名・地域名のみを店名として検出")
        elif headline_noise_name:
            reasons.append("見出しの煽り文・説明文を店名として検出")
        else:
            reasons.append("一般語・文章断片を店名として検出")
        return "C", "保存のみ", min(score, 3), reasons
    if not strong_geo:
        reasons.append("記事本文で対象地域の強い根拠を確認できない")
        score = min(score, 7)
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
    same_context = unnamed or store_location_status_same_context(text, sig.store_name, sig.area)
    if not same_context:
        reasons.append("店名・所在地・開閉店情報が同一文脈で確認できない")
    if specific_area and geo_ok and strong_geo and a_name and a_current and same_context and score >= 10:
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
    display_items = [x for x in unconfirmed if x.review_class == "A" and x.area in TARGET_AREAS]

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
    log.info("=== Ver.7.3.8.1 札幌・函館 深掘りシグナル収集 ===")
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
