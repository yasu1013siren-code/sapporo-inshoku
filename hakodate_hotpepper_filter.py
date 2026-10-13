"""Create a Hakodate-selected feed without deleting collector data.

Review records require explicit evidence; search failure is never absence.
Run: python hakodate_hotpepper_filter.py --news news.json --reviews hotpepper_reviews.json --output news_selected.json
"""
import argparse
import copy
import json
import unicodedata
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

JST = timezone(timedelta(hours=9))

def normalize(value):
    return ''.join(unicodedata.normalize('NFKC', value or '').split()).casefold()

def identity(item):
    return normalize(item.get('name') or item.get('title')), normalize(item.get('place'))

def select(payload, reviews, now=None, max_age_days=7):
    now = now or datetime.now(JST)
    if now.tzinfo is None:
        raise ValueError('now must include timezone')
    result = copy.deepcopy(payload)
    indexed = {}
    for review in reviews:
        indexed.setdefault(identity(review), []).append(review)
        if review.get('verified_address'):
            alias=identity(dict(review,place=review['verified_address']))
            if alias != identity(review):
                indexed.setdefault(alias, []).append(review)
    kept, excluded, pending = [], [], []
    candidates = result.get('hakodate_directory_candidates', result.get('areas', {}).get('hakodate', []))
    seen = set()
    for item in copy.deepcopy(candidates):
        if item.get('type') == 'closed' or identity(item) in seen:
            continue
        seen.add(identity(item))
        records = indexed.get(identity(item), [])
        # Duplicate or conflicting records require review, never arbitrary selection.
        record = records[0] if len(records) == 1 else None
        status = 'unknown'
        if record:
            try:
                checked = datetime.fromisoformat(record['checked_at'])
                age = now - checked if checked.tzinfo else None
                fresh = age is not None and timedelta(0) <= age <= timedelta(days=max_age_days)
            except (ValueError, KeyError, TypeError):
                fresh = False
            urls = record.get('evidence_urls', [])
            evidence = isinstance(urls, list) and bool(urls) and all(
                isinstance(u, str) and urlparse(u).scheme == 'https' and
                ((urlparse(u).hostname or '') == 'www.hotpepper.jp' or (urlparse(u).hostname or '') == 'hotpepper.jp') for u in urls)
            complete = record.get('name_variants_checked') is True and record.get('address_checked') is True
            if fresh and evidence and record.get('status') == 'listed':
                status = 'listed'
            elif fresh and evidence and complete and record.get('status') == 'not_found':
                status = 'not_found'
        if status == 'not_found':
            item['hotpepper_check'] = {'status': status, 'checked_at': record['checked_at'],
                'label': '確認日時点で掲載なし', 'evidence_urls': record['evidence_urls']}
            kept.append(item)
        elif status == 'listed':
            excluded.append(item)
        else:
            pending.append(item)
    result['hakodate_directory'] = kept
    result['hakodate_selection'] = {'checked_at': now.isoformat(), 'max_age_days': max_age_days,
        'included': len(kept), 'excluded_listed': len(excluded), 'pending': len(pending)}
    return result, {'pending': pending, 'excluded_listed': excluded}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--news', required=True)
    parser.add_argument('--reviews', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--review-output', default='hakodate_pending.json')
    args = parser.parse_args()
    if len({args.news, args.reviews, args.output, args.review_output}) != 4:
        parser.error('input and output paths must be different')
    with open(args.news, encoding='utf-8') as f:
        payload = json.load(f)
    with open(args.reviews, encoding='utf-8') as f:
        reviews = json.load(f)
    result, pending = select(payload, reviews)
    for path, value in ((args.output, result), (args.review_output, pending)):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)

if __name__ == '__main__':
    main()
