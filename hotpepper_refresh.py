"""Conservative live Hot Pepper keyword checks; errors remain pending."""
import html
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from hakodate_hotpepper_filter import JST, identity, normalize, select

def fetch(url):
    result=subprocess.run(['curl','--fail','--silent','--show-error','--location',
        '--max-time','25','--user-agent','SapporoRestaurantInfo/1.0',url],
        capture_output=True, timeout=28, check=True)
    return result.stdout.decode('utf-8')

def search(query):
    url = 'https://www.hotpepper.jp/SA41/Y883/ff/?' + urlencode({'FWT': query})
    page = fetch(url)
    values = re.findall(r'<input\b[^>]*\bname=["\']FWT["\'][^>]*>', page, re.I)
    echoed = any(normalize(html.unescape(re.search(r'value=["\']([^"\']*)', tag).group(1))) == normalize(query)
                 for tag in values if re.search(r'value=["\']([^"\']*)',tag))
    match = re.search(r'検索結果</span>\s*<span[^>]*>([\d,]+)</span>件', page)
    if not echoed or not match:
        raise ValueError('search query or result count could not be verified')
    count = int(match.group(1).replace(',', ''))
    return url, count

def check(item, prior):
    result = dict(prior or {}, name=item.get('name') or item.get('title'), place=item.get('place'),
                  attempted_at=datetime.now(JST).isoformat())
    name = result['name']
    variants = list(dict.fromkeys(result.get('name_variants', []) or [name, normalize(name)]))
    # Reuse a recent completed review; failed attempts are retried next run.
    try:
        checked=datetime.fromisoformat(result.get('checked_at',''))
        if result.get('status') in ('listed','not_found') and checked.tzinfo and timedelta(0) <= datetime.now(JST)-checked <= timedelta(days=1):
            return result
    except (ValueError,TypeError):
        pass
    if result.get('status') == 'listed':
        try:
            page=fetch(result['evidence_urls'][0])
            text=normalize(html.unescape(re.sub(r'<[^>]+>', ' ', page)))
            address=normalize(result.get('verified_address',''))
            aliases=result.get('name_variants', [name])
            if address and address in text and any(normalize(alias) in text for alias in aliases):
                result['checked_at']=datetime.now(JST).isoformat()
                return result
        except Exception:
            pass
        result.update(status='unknown',reason='listing_recheck_required')
        return result
    try:
        evidence=[]
        counts=[]
        for query in variants:
            url,count=search(query)
            evidence.append(url); counts.append(count)
        address = result.get('verified_address') or (item.get('place','') if item.get('address_verified') else '')
        if address:
            result['verified_address']=address
        address_ok = bool(re.search(r'函館市[^\d]+\d+[-－丁目番地号\d]+', address))
        if address_ok:
            url,count=search(address.replace('北海道',''))
            evidence.append(url); counts.append(count)
        # Internal zero counts alone never establish absence. External identity
        # review must be recorded separately; do not inherit an old negative check.
        result.update(status='unknown',
            checked_at=datetime.now(JST).isoformat(), evidence_urls=evidence,
            result_counts=counts, name_variants_checked=True, address_checked=address_ok,
            reason='external_search_review_required' if address_ok and all(c == 0 for c in counts) else 'requires_identity_review')
        result.pop('error',None)
    except Exception as exc:
        result.update(status='unknown',reason='search_failed',error=type(exc).__name__)
    return result

def apply_live_selection(payload, base_dir):
    base_dir=Path(base_dir)
    path=base_dir/'hotpepper_reviews.json'
    old=json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    indexed={identity(r):r for r in old}
    from hakodate_directory import collect_directory
    events=payload.get('areas',{}).get('hakodate',[])
    candidates=collect_directory(base_dir)
    indexed_candidates={identity(item):item for item in candidates}
    for item in events:
        if item.get('type') != 'closed':
            indexed_candidates.setdefault(identity(item),item)
    items=list(indexed_candidates.values())
    payload=dict(payload,hakodate_directory_candidates=items)
    # Bound each daily batch; all remaining stores stay pending.
    ordered=sorted(items,key=lambda item:indexed.get(identity(item),{}).get('attempted_at',''))
    batch=ordered[:30]
    with ThreadPoolExecutor(max_workers=3) as pool:
        records=list(pool.map(lambda item:check(item,indexed.get(identity(item))),batch))
    records += [indexed.get(identity(item),dict(item,status='unknown',reason='batch_limit')) for item in ordered[30:]]
    # Keep records for temporarily missing stores, without refreshing their check date.
    keys={identity(r) for r in records}
    records += [r for r in old if identity(r) not in keys]
    path.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    result,queue=select(payload,records)
    for item in result.get('hakodate_directory',[]):
        record=next(r for r in records if identity(r)==identity(item))
        if record.get('verified_address'):
            item['place']=record['verified_address']
    (base_dir/'hakodate_pending.json').write_text(json.dumps(queue,ensure_ascii=False,indent=2),encoding='utf-8')
    return result

if __name__=='__main__':
    base=Path(__file__).resolve().parent
    payload=json.loads((base/'news.json').read_text(encoding='utf-8'))
    result=apply_live_selection(payload,base)
    (base/'news.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result['hakodate_selection'],ensure_ascii=False))
