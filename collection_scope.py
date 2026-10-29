"""Public feed scope requested on 2026-10-29; directory ledgers are untouched."""
import re

TARGET_AREAS = {"chuo", "kita", "higashi", "nishi", "teine", "otaru", "hakodate"}
NON_FOOD = re.compile(r"美容室|美容院|ネイルサロン|整骨院|歯科|薬局|ドラッグストア|アパレル|古着|雑貨|バラエティショップ|THE BODY SHOP|キャッツアイ|^アーバンネット札幌リンクタワー$", re.I)

def excluded_event(item):
    if item.get("type", item.get("status")) == "closed":
        return True
    subject = " ".join(str(item.get(k) or "") for k in ("name", "title"))
    return bool(NON_FOOD.search(subject))

def apply_collection_scope(payload):
    for area, rows in payload.get("areas", {}).items():
        if area in TARGET_AREAS:
            payload["areas"][area] = [row for row in rows if not excluded_event(row)]
    return payload
