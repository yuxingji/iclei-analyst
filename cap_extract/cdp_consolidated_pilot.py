"""Select the CDP pilot offline, using only city names from the 2025 data."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from openpyxl import load_workbook
from .cdp_consolidated_prepare import ROOT,OUT,PUBLIC_2025


def city_key(name: str, state: str):
    text=re.sub(r'[^a-z0-9 ]',' ',str(name or '').casefold())
    text=re.sub(r'\b(?:city|town|village|township|borough)\s+of\s+','',text)
    return (' '.join(text.split()),str(state or '').strip().upper())


def public_city_names(path: Path):
    """Read only the public Summary country and organization name columns."""
    workbook=load_workbook(path,read_only=True,data_only=True)
    iterator=workbook['Summary'].iter_rows(values_only=True)
    header=next(iterator); index={name:i for i,name in enumerate(header)}
    result=set()
    for row in iterator:
        if row[index['discloser_country_or_area']]!='United States of America':
            continue
        name=str(row[index['disclosing_organization']] or '').strip()
        match=re.search(r',\s*([A-Z]{2})\s*$',name)
        if match:result.add(city_key(name[:match.start()],match.group(1)))
    workbook.close()
    return result


def load_candidates(db: Path, names):
    uri=db.resolve().as_uri()+'?mode=ro'
    c=sqlite3.connect(uri,uri=True);c.row_factory=sqlite3.Row
    # Aggregate once before joining: the unindexed multi-table join can take
    # minutes on the full extraction database.
    page_counts={r['plan_id']:r['n'] for r in c.execute('SELECT plan_id,COUNT(*) AS n FROM pages WHERE n_chars>100 GROUP BY plan_id')}
    exports={r['plan_id']:r['payload'] for r in c.execute("SELECT plan_id,payload FROM exports_v23 WHERE table_name='Plan Level'")}
    result=[]
    for row in c.execute('SELECT plan_id,jurisdiction,state,gov_type,source_file,sha256 FROM plans ORDER BY plan_id'):
        if row['plan_id'] not in exports or row['plan_id'] not in page_counts:continue
        data=json.loads(exports[row['plan_id']]);year=data.get('plan_publish_year')
        try:year=int(year)
        except (ValueError,TypeError):continue
        gov=str(row['gov_type'] or '');orientation=str(data.get('plan_orientation_controlled') or '')
        key=city_key(row['jurisdiction'],row['state'])
        pdf_id='PDF-'+hashlib.sha256((row['source_file']+'\0'+str(row['sha256'] or '')).encode()).hexdigest()[:16]
        result.append({'pdf_id':pdf_id,'plan_id':row['plan_id'],'municipality':row['jurisdiction'],
                       'state':row['state'],'gov_type':gov,'source_file':row['source_file'],
                       'disclosure_year':year,'orientation':orientation,'pages_with_text':page_counts[row['plan_id']],
                       'in_cdp_2025':key in names,'_municipality_key':key})
    c.close();return result


def select_pilots(candidates):
    """Deterministic greedy selection with checked diversity constraints."""
    selected=[];used_cities=set()
    def choose(predicate):
        choices=[r for r in candidates if r['_municipality_key'] not in used_cities and predicate(r)]
        if not choices:raise ValueError('Cannot satisfy pilot diversity constraints')
        choices.sort(key=lambda r:(not r['in_cdp_2025'],r['state'] in {x['state'] for x in selected},
                                   r['gov_type'] in {x['gov_type'] for x in selected},
                                   r['orientation'] in {x['orientation'] for x in selected},
                                   not 2023<=r['disclosure_year']<=2025,
                                   hashlib.sha256(('cdp-pilot-42:'+r['plan_id']).encode()).hexdigest()))
        chosen=choices[0];selected.append(chosen);used_cities.add(chosen['_municipality_key'])
    for _ in range(2):choose(lambda r:r['disclosure_year']<2015)
    for _ in range(4):choose(lambda r:r['disclosure_year']>=2020)
    if not any('county' in r['gov_type'].casefold() for r in selected):
        choose(lambda r:'county' in r['gov_type'].casefold())
    if not any(any(x in r['gov_type'].casefold() for x in ('town','village')) for r in selected):
        choose(lambda r:any(x in r['gov_type'].casefold() for x in ('town','village')))
    while len(selected)<10:choose(lambda r:True)
    if sum(r['in_cdp_2025'] for r in selected)<5:
        raise ValueError('Fewer than five state-and-name matches were selected; inspect matches')
    for row in selected:row.pop('_municipality_key')
    return selected


def main():
    names=public_city_names(PUBLIC_2025)
    plans=select_pilots(load_candidates(ROOT/'outputs'/'cap_extract'/'cap.sqlite',names))
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'consolidated_pilot_manifest.json').write_text(json.dumps(plans,ensure_ascii=False,indent=2),encoding='utf8')
    for p in plans:print(json.dumps(p,ensure_ascii=False))
    print(f"plans={len(plans)} cdp_name_state_matches={sum(p['in_cdp_2025'] for p in plans)}")


if __name__=='__main__':main()
