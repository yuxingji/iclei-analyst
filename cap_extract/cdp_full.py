"""Freeze the text-bearing corpus and exclude scans without modifying CAP data."""
import hashlib,json,sqlite3
from .cdp_consolidated_pilot import load_candidates
from .cdp_consolidated_prepare import ROOT,OUT

MANIFEST=OUT/'consolidated_full_manifest.json'
EXCLUDED=OUT/'consolidated_full_excluded.json'

def current_manifest():
    db=ROOT/'outputs'/'cap_extract'/'cap.sqlite'
    plans=load_candidates(db,set());by_id={p['plan_id']:p for p in plans}
    for p in plans:p.pop('_municipality_key',None)
    con=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
    digests={pid:hashlib.sha256() for pid in by_id}
    for pid,page,text in con.execute('SELECT plan_id,page_no,text FROM pages ORDER BY plan_id,page_no'):
        if pid in digests:
            digests[pid].update(str(page).encode()+b'\0'+str(text or '').encode('utf8')+b'\0')
    for p in plans:p['page_text_sha256']=digests[p['plan_id']].hexdigest()
    excluded=[{'plan_id':pid,'jurisdiction':j,'state':s,'reason':'No usable indexed text; OCR pending'}
              for pid,j,s in con.execute('SELECT plan_id,jurisdiction,state FROM plans ORDER BY plan_id') if pid not in by_id]
    con.close()
    if len({p['pdf_id'] for p in plans})!=len(plans):raise ValueError('PDF identifier collision')
    return sorted(plans,key=lambda p:p['plan_id']),excluded

def load_or_create_manifest():
    current,excluded=current_manifest()
    if MANIFEST.exists():
        saved=json.loads(MANIFEST.read_text(encoding='utf8'))
        if saved!=current:raise ValueError('Corpus identity or indexed page text changed; preserve the saved manifest and review before resuming')
        return saved
    OUT.mkdir(parents=True,exist_ok=True)
    MANIFEST.write_text(json.dumps(current,ensure_ascii=False,indent=2),encoding='utf8')
    EXCLUDED.write_text(json.dumps(excluded,ensure_ascii=False,indent=2),encoding='utf8')
    return current

if __name__=='__main__':
    plans=load_or_create_manifest()
    excluded=json.loads(EXCLUDED.read_text(encoding='utf8')) if EXCLUDED.exists() else []
    print(f'Frozen text-bearing PDFs: {len(plans)}; OCR/no-text excluded: {len(excluded)}')
