"""Merge evidence-reviewed updates without network calls or fabricated dates."""
import json
from pathlib import Path

WARDS = ("chuo", "kita", "higashi", "shiroishi", "atsubetsu",
         "toyohira", "kiyota", "minami", "nishi", "teine")

def external_candidate(item):
    """Human-reviewed external absence is a candidate, never confirmed absence."""
    review = item.get("identity_review", {})
    return (item.get("status") == "external_not_found" and
            review.get("decision") == "external_not_found" and
            item.get("external_search_checked") is True and
            bool(item.get("phone")) and bool(item.get("url")) and
            len(set(item.get("search_queries", []))) >= 2 and
            all(review.get(key) is True for key in
                ("name_variants_checked", "address_checked", "phone_checked")) and
            review.get("listing_signal") is False)

def apply_verified_updates(payload, base_dir):
    base = Path(base_dir)
    path = base / "verified_information_updates.json"
    if not path.exists():
        return payload
    updates = json.loads(path.read_text(encoding="utf-8"))
    # Only add explicitly reviewed event rows; never replace another region.
    for event in updates.get("events", []):
        area = event.get("area")
        if area not in (*WARDS, "hakodate"):
            continue
        rows = payload.setdefault("areas", {}).setdefault(area, [])
        if not any(row.get("name") == event.get("name") and
                   row.get("place") == event.get("place") and
                   row.get("date") == event.get("date") for row in rows):
            rows.append(dict(event))
    path = base / "sapporo_directory_candidates.json"
    candidates = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    payload["sapporo_directory_candidates"] = candidates
    # No inferred negative checks: only manually reviewed, fully evidenced records.
    payload["sapporo_directory"] = {ward: [] for ward in WARDS}
    for item in candidates:
        if (item.get("area") in WARDS and item.get("address_verified") and
            (external_candidate(item) or
             (item.get("status") == "not_found" and item.get("external_search_checked") and
              item.get("identity_review", {}).get("decision") == "not_found"))):
            row = dict(item)
            row["hotpepper_check"] = {key: item[key] for key in
                ("status", "checked_at", "evidence_urls", "identity_review",
                 "external_search_checked") if key in item}
            payload["sapporo_directory"][item["area"]].append(row)
    payload["directory_research"] = updates.get("research", {})
    return payload
