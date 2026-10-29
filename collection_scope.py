"""Public feed scope requested on 2026-10-29; directory ledgers are untouched."""
import re
import json
from pathlib import Path

TARGET_AREAS = {"chuo", "kita", "higashi", "nishi", "teine", "otaru", "hakodate"}
NON_FOOD = re.compile(r"美容室|美容院|ネイルサロン|整骨院|歯科|薬局|ドラッグストア|アパレル|古着|雑貨|バラエティショップ|THE BODY SHOP|キャッツアイ|^アーバンネット札幌リンクタワー$", re.I)

def excluded_event(item):
    if item.get("type", item.get("status")) == "closed":
        return True
    subject = " ".join(str(item.get(k) or "") for k in ("name", "title"))
    return bool(NON_FOOD.search(subject))

def apply_collection_scope(payload):
    price_path = Path(__file__).with_name("price_exclusions.json")
    exclusions = json.loads(price_path.read_text(encoding="utf-8")) if price_path.exists() else []
    excluded_names = {item["name"] for item in exclusions}
    payload["price_exclusions"] = exclusions
    payload["hakodate_directory"] = [row for row in payload.get("hakodate_directory", []) if row.get("name") not in excluded_names]
    payload["otaru_directory"] = [row for row in payload.get("otaru_directory", []) if row.get("name") not in excluded_names]
    for area, rows in payload.get("sapporo_directory", {}).items():
        payload["sapporo_directory"][area] = [row for row in rows if row.get("name") not in excluded_names]
    for area, rows in payload.get("areas", {}).items():
        if area in TARGET_AREAS:
            payload["areas"][area] = [row for row in rows if not excluded_event(row) and row.get('name') not in excluded_names]
    return payload
