from __future__ import annotations
import csv, hashlib, json, re, sqlite3, time, traceback
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path.cwd()
OUT = ROOT / "outputs" / "cap_extract"
DB = OUT / "cap.sqlite"
TEMPLATE = ROOT / "CAP_PDF_Structured_Extraction_Template_v2_1.xlsx"
PDF_DIR = ROOT / "outputs" / "iclei_cap" / "pdf"
ATLAS = ROOT / "outputs" / "iclei_cap" / "atlas_inventory.csv"
SCHEMA_CONFIG = Path(__file__).parent / 'schema_v2_3.yaml'

@lru_cache(maxsize=1)
def schema_config():
    import yaml
    with SCHEMA_CONFIG.open(encoding='utf-8') as f:
        return yaml.safe_load(f)

_CONFIG = schema_config()
SCHEMA = dict(_CONFIG['schemas'])
SCHEMA['Evidence Log'] = ['evidence_id','plan_id','dimension_bucket','variable_candidate','source_page','source_section','evidence_text','structured_interpretation','confidence','review_status','notes']

def connect():
    OUT.mkdir(parents=True, exist_ok=True)
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    c.executescript('''CREATE TABLE IF NOT EXISTS plans(plan_id TEXT PRIMARY KEY,state TEXT,jurisdiction TEXT,gov_type TEXT,plan_year INTEGER,plan_title TEXT,source_file TEXT,source_path TEXT,sha256 TEXT,source_url TEXT,duplicate_files TEXT,status_inventory TEXT DEFAULT 'done',timing REAL,status_llm TEXT,failed_pages TEXT);
    CREATE TABLE IF NOT EXISTS pages(plan_id TEXT,page_no INTEGER,text TEXT,n_chars INTEGER,PRIMARY KEY(plan_id,page_no));
    CREATE TABLE IF NOT EXISTS page_hits(plan_id TEXT,page_no INTEGER,hits_json TEXT,relevance REAL,action_table INTEGER,PRIMARY KEY(plan_id,page_no));
    CREATE TABLE IF NOT EXISTS rule_features(plan_id TEXT PRIMARY KEY,features_json TEXT);
     CREATE TABLE IF NOT EXISTS llm_raw(id INTEGER PRIMARY KEY,plan_id TEXT,task TEXT,model TEXT,prompt_hash TEXT,created_at TEXT,duration REAL,response TEXT,error TEXT,options TEXT);
     CREATE TABLE IF NOT EXISTS llm_runs(run_id TEXT PRIMARY KEY,provider TEXT,requested_model TEXT,actual_model TEXT,started_at TEXT,ended_at TEXT,input_cached INTEGER DEFAULT 0,input_uncached INTEGER DEFAULT 0,output_tokens INTEGER DEFAULT 0,reasoning_tokens INTEGER DEFAULT 0,reasoning_cost_usd REAL DEFAULT 0,cost_usd REAL DEFAULT 0,cost_discounted_usd REAL DEFAULT 0,api_calls INTEGER DEFAULT 0,retries INTEGER DEFAULT 0,status TEXT,schema_hash TEXT);
     CREATE TABLE IF NOT EXISTS llm_prompt_versions(run_id TEXT,task TEXT,prompt_hash TEXT,PRIMARY KEY(run_id,task));
    CREATE TABLE IF NOT EXISTS extracted(plan_id TEXT,task TEXT,payload TEXT,PRIMARY KEY(plan_id,task));
     CREATE TABLE IF NOT EXISTS extracted_archive(plan_id TEXT,schema_hash TEXT,task TEXT,payload TEXT,archived_at TEXT,PRIMARY KEY(plan_id,schema_hash,task));
     CREATE TABLE IF NOT EXISTS plan_schema_versions(plan_id TEXT PRIMARY KEY,schema_hash TEXT,status TEXT,updated_at TEXT);
     CREATE TABLE IF NOT EXISTS schema_baselines(plan_id TEXT,schema_version TEXT,from_schema_hash TEXT,action_rows INTEGER,risk_rows INTEGER,created_at TEXT,PRIMARY KEY(plan_id,schema_version));
    CREATE TABLE IF NOT EXISTS stage_status(plan_id TEXT,stage TEXT,status TEXT,updated_at TEXT,PRIMARY KEY(plan_id,stage));
     CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY,plan_id TEXT,stage TEXT,file_path TEXT,error TEXT,traceback TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS exports(table_name TEXT,payload TEXT);
     CREATE TABLE IF NOT EXISTS exports_v23(table_name TEXT,plan_id TEXT,record_key TEXT,payload TEXT,PRIMARY KEY(table_name,plan_id,record_key));
     ''');
    cols={r[1] for r in c.execute('PRAGMA table_info(plans)')}
    if 'status_llm' not in cols: c.execute('ALTER TABLE plans ADD COLUMN status_llm TEXT')
    if 'failed_pages' not in cols: c.execute('ALTER TABLE plans ADD COLUMN failed_pages TEXT')
    cols={r[1] for r in c.execute('PRAGMA table_info(errors)')}
    if 'traceback' not in cols: c.execute('ALTER TABLE errors ADD COLUMN traceback TEXT')
    cols={r[1] for r in c.execute('PRAGMA table_info(llm_raw)')}
    if 'options' not in cols: c.execute('ALTER TABLE llm_raw ADD COLUMN options TEXT')
    for name,decl in [('run_id','TEXT'),('provider','TEXT'),('actual_model','TEXT'),('input_cached','INTEGER'),('input_uncached','INTEGER'),('output_tokens','INTEGER'),('reasoning_tokens','INTEGER DEFAULT 0'),('reasoning_cost_usd','REAL DEFAULT 0'),('cost_usd','REAL'),('retry_count','INTEGER'),('api_calls','INTEGER')]:
        if name not in cols: c.execute(f'ALTER TABLE llm_raw ADD COLUMN {name} {decl}')
    run_cols={r[1] for r in c.execute('PRAGMA table_info(llm_runs)')}
    for name,decl in [('api_calls','INTEGER DEFAULT 0'),('retries','INTEGER DEFAULT 0'),('reasoning_tokens','INTEGER DEFAULT 0'),('reasoning_cost_usd','REAL DEFAULT 0'),('schema_hash','TEXT')]:
        if name not in run_cols: c.execute(f'ALTER TABLE llm_runs ADD COLUMN {name} {decl}')
    c.commit(); return c

def mark(c,pid,stage,status):
    c.execute('INSERT OR REPLACE INTO stage_status VALUES(?,?,?,datetime(\'now\'))',(pid,stage,status)); c.commit()
def done(c,pid,stage):
    r=c.execute('SELECT status FROM stage_status WHERE plan_id=? AND stage=?',(pid,stage)).fetchone(); return bool(r and r[0]=='done')
def error(c,pid,stage,path,e):
    c.execute('INSERT INTO errors(plan_id,stage,file_path,error,traceback,created_at) VALUES(?,?,?,?,?,datetime(\'now\'))',(pid,stage,str(path),str(e),traceback.format_exc())); c.commit()
def template_data():
    from openpyxl import load_workbook
    w=load_workbook(TEMPLATE,data_only=True,read_only=True)
    sheets={}
    for name in ('Variable Dictionary','Controlled Values','Extraction Protocol'):
        ws=w[name]; rows=list(ws.values); sheets[name]=rows
    cols={}
    for name in SCHEMA:
        if name in _CONFIG['schemas']:
            cols[name]=list(_CONFIG['schemas'][name])
        else:
            ws=w[name]; cols[name]=list(next(ws.values))
    # Risk Detail preserves the schema-v2.2 one-row-per-risk fields; Risk Level
    # is the separate plan-by-orientation aggregation in v2.3 exports.
    cols['Risk Detail']=list(cols['Risk Level'])
    dictionary=list(sheets['Variable Dictionary'])
    valid_fields={field for sheet,fields in _CONFIG['schemas'].items() for field in fields}
    header=dictionary[0] if dictionary else ('Workbook','Variable','Field','Level','Definition','Allowed/Format','Rule','Source','Notes','Example')
    dictionary=[header]+[row for row in dictionary[1:] if len(row)>2 and row[2] in valid_fields]
    changed_definitions=dict(_CONFIG['fields'])
    sheet_for={field:sheet for sheet,fields in _CONFIG['schemas'].items() for field in fields}
    for field in sorted(valid_fields):
        metadata=changed_definitions.get(field,{'definition':field.replace('_',' '),'allowed':'free text'})
        allowed=metadata.get('allowed','free text')
        if isinstance(allowed,list): allowed=' | '.join(map(str,allowed))
        level='Plan Level' if sheet_for[field]=='Plan Level' else 'Action Level'
        replacement=(level,f"Schema v{_CONFIG['version']}",field,level,metadata.get('definition',field.replace('_',' ')),allowed,'Evidence-first extraction; use field-specific evidence and preserve explicit missingness.',None,'Raw structured extraction; no scores or indices.',None)
        idx=next((i for i,row in enumerate(dictionary) if len(row)>2 and row[2]==field),None)
        if idx is None: dictionary.append(replacement)
        else: dictionary[idx]=replacement
    sheets['Variable Dictionary']=dictionary
    control=list(sheets['Controlled Values'])
    new_controls=[]
    for field,metadata in changed_definitions.items():
        if isinstance(metadata.get('allowed'),list):
            new_controls.append((field,' | '.join(map(str,metadata['allowed'])),metadata['definition']))
        elif field in {'hazards','hazard_normalized'}:
            import yaml
            hazards=yaml.safe_load((Path(__file__).parent/'dictionaries.yaml').read_text(encoding='utf-8')).get('hazards',{})
            new_controls.append((field,' | '.join(hazards),metadata['definition']))
    control_header=control[0] if control else ('Variable','Allowed Values','Definition')
    control=[control_header]+new_controls
    sheets['Controlled Values']=control
    protocol=list(sheets['Extraction Protocol'])
    protocol=[protocol[0]] if protocol else ('Protocol','Instruction','Notes')
    for task,text in _CONFIG['prompts'].items():
        protocol.append((f"Schema v{_CONFIG['version']} {task}",text,'Final researcher extraction rule.'))
    sheets['Extraction Protocol']=protocol
    return cols,sheets
def clean(s):
    s=re.sub(r'-\s*\n\s*','',s or '')
    return re.sub(r'\s+',' ',s).strip()
def split_filename(name):
    parts=Path(name).stem.split('__')
    if len(parts)<6: raise ValueError('Filename does not match expected state/type/issuer/year/title/hash pattern')
    state,gov,urlhash=parts[0],parts[1],parts[-1]
    years=[i for i,x in enumerate(parts[2:-1],2) if re.fullmatch(r'(?:19|20)\d{2}',x)]
    if not years: raise ValueError('Filename has no four-digit year field')
    yi=years[0]
    if yi<=2 or yi>=len(parts)-2: raise ValueError('Filename has empty issuer or title')
    issuer='__'.join(parts[2:yi]); year=parts[yi]; title='__'.join(parts[yi+1:-1])
    return state,gov,issuer,year,title,urlhash
def inventory(c,force=False,limit=None,only=None):
    urls={}
    if ATLAS.exists():
        with ATLAS.open(encoding='utf-8-sig',newline='') as f:
            for r in csv.DictReader(f): urls[str(r.get('source_url',''))[-10:]]=r.get('source_url','')
    files=sorted(PDF_DIR.glob('*.pdf')) if PDF_DIR.exists() else []
    seen={}; n=0
    for p in files:
        try:
            st,gov,issuer,yr,title,h=split_filename(p.name); sha=hashlib.sha256(p.read_bytes()).hexdigest()
            if sha in seen:
                pid=seen[sha]
                row=c.execute('SELECT duplicate_files,source_file FROM plans WHERE plan_id=?',(pid,)).fetchone()
                if row:
                    dup=set(json.loads(row[0] or '[]')); dup.add(p.name)
                    c.execute('UPDATE plans SET duplicate_files=? WHERE plan_id=?',(json.dumps(sorted(dup)),pid)); c.commit()
                continue
            juris=re.sub(r'[^A-Za-z0-9]','',issuer.split()[-1])[:12] or 'Unknown'; suffix_len=6
            pid=f'{st}_{juris}_{yr}_{sha[:suffix_len]}'
            while c.execute('SELECT 1 FROM plans WHERE plan_id=? AND sha256<>?',(pid,sha)).fetchone():
                suffix_len+=2; pid=f'{st}_{juris}_{yr}_{sha[:suffix_len]}'
            seen[sha]=pid
            if only and pid not in only: continue
            if done(c,pid,'inventory') and not force: continue
            c.execute('INSERT OR REPLACE INTO plans(plan_id,state,jurisdiction,gov_type,plan_year,plan_title,source_file,source_path,sha256,source_url,duplicate_files,status_inventory) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(pid,st,issuer,gov,int(yr) if yr.isdigit() else None,title,p.name,str(p.resolve()),sha,urls.get(h,''),'[]','done'))
            mark(c,pid,'inventory','done'); n+=1
            if limit and n>=limit: break
        except Exception as e: error(c,None,'inventory',p,e)
    return n
