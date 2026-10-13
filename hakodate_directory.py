"""Date-independent restaurant discovery from Hakodate's official tourism directory.
Rotates detail pages across runs and retains previously discovered candidates.
"""
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from hakodate_hotpepper_filter import JST, identity

ROOT = 'https://www.hakobura.jp'
START = ROOT + '/spots?q%5Bcategories_parent_category_id_in%5D=10'

def parse_spot(page, url):
    doc=BeautifulSoup(page,'lxml')
    heading=doc.select_one('h1')
    address=''
    for row in doc.select('tr'):
        label=row.select_one('th'); value=row.select_one('td')
        if label and value and label.get_text(strip=True)=='住所':
            address=value.get_text(' ',strip=True)
            break
    if heading and re.search(r'釣堀|釣り体験|市場$|横丁$|ミュージアム|Museum',heading.get_text()):
        return None
    if not heading or not re.search(r'函館市[^\d]+\d',address):
        return None
    # Tourism listings include experiences; only food listings with restaurant clues qualify.
    text=doc.get_text(' ',strip=True)
    if not re.search(r'レストラン|食堂|カフェ|喫茶|居酒屋|ラーメン|そば|寿司|鮨|料理|飲食店|バー',text):
        return None
    return {'name':heading.get_text(' ',strip=True),'place':address,'address_verified':True,
            'type':'restaurant','date':'','url':url,'source':'函館市公式観光サイト はこぶら',
            'last_seen':datetime.now(JST).isoformat()}

def collect_directory(base_dir, max_pages=5, max_details=30):
    from collector_ver72_hakodate import fetch_text, is_allowed_by_robots
    base=Path(base_dir); path=base/'hakodate_directory_candidates.json'
    saved=json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    items={identity(i):i for i in saved}
    state_path=base/'hakodate_directory_state.json'
    state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    page_url=state.get('next_page') or START
    links=list(state.get('links',[])); errors=0
    for _ in range(max_pages):
        if not is_allowed_by_robots(page_url):
            errors+=1; break
        page=fetch_text(page_url)
        if not page:
            errors+=1; break
        doc=BeautifulSoup(page,'lxml')
        for a in doc.select('a[href]'):
            href=urljoin(ROOT,a['href']); parsed=urlsplit(href)
            if parsed.hostname=='www.hakobura.jp' and re.fullmatch(r'/spots/\d+',parsed.path):
                url=ROOT+parsed.path
                if url not in links: links.append(url)
        nxt=doc.select_one('a[rel="next"]')
        page_url=urljoin(ROOT,nxt['href']) if nxt else START
        if page_url==START: break
    attempts=state.get('attempts',{})
    for url in sorted(links,key=lambda u:attempts.get(u,''))[:max_details]:
        attempts[url]=datetime.now(JST).isoformat()
        if not is_allowed_by_robots(url): errors+=1; continue
        page=fetch_text(url)
        if not page: errors+=1; continue
        item=parse_spot(page,url)
        if item: items[identity(item)]=item
    path.write_text(json.dumps(list(items.values()),ensure_ascii=False,indent=2),encoding='utf-8')
    state_path.write_text(json.dumps({'next_page':page_url,'attempts':attempts,'links':links,'errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
    return list(items.values())
