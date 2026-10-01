from __future__ import annotations
import hashlib,json,re,time,statistics
from pathlib import Path
from functools import lru_cache
from .core import *

def selected(c,only=None,limit=None):
    q='SELECT * FROM plans ORDER BY plan_id'; rows=c.execute(q).fetchall()
    if only: rows=[r for r in rows if r['plan_id'] in only]
    return rows[:limit] if limit else rows

def text_stage(c,force=False,limit=None,only=None):
    import fitz
    n=0
    for plan in selected(c,only,limit):
        pid=plan['plan_id']
        if done(c,pid,'text') and not force: continue
        mark(c,pid,'text','running')
        try:
            doc=fitz.open(plan['source_path'])
            if len(doc)==0: raise ValueError('PDF contains zero pages')
            pages=[clean(p.get_text('text')) for p in doc]
            avg=sum(map(len,pages))/max(1,len(pages))
            if avg<200:
                import shutil,subprocess,tempfile
                ocr=shutil.which('ocrmypdf')
                if not ocr:
                    mark(c,pid,'text','needs_ocr'); c.execute('UPDATE plans SET status_inventory=status_inventory WHERE plan_id=?',(pid,)); c.commit(); continue
                with tempfile.TemporaryDirectory() as td:
                    out=Path(td)/'ocr.pdf'; subprocess.run([ocr,'--skip-text',plan['source_path'],str(out)],check=True,capture_output=True)
                    doc=fitz.open(out); pages=[clean(p.get_text('text')) for p in doc]
            c.execute('DELETE FROM pages WHERE plan_id=?',(pid,)); c.executemany('INSERT INTO pages VALUES(?,?,?,?)',[(pid,i+1,t,len(t)) for i,t in enumerate(pages)])
            mark(c,pid,'text','done'); n+=1
        except Exception as e: error(c,pid,'text',plan['source_path'],e)
    return n

@lru_cache(maxsize=1)
def _target_patterns():
    import yaml
    cfg=yaml.safe_load((Path(__file__).parent/'dictionaries.yaml').read_text(encoding='utf-8'))
    return [re.compile(p,re.I) for p in cfg['target_patterns']]

def target_hits(text,patterns=None):
    patterns=patterns or _target_patterns()
    out=[]
    for pat in patterns:
        for m in pat.finditer(text):
            a,b=m.span(); sentence=re.search(r'[^.!?\n]*'+re.escape(m.group(0))+r'[^.!?\n]*',text[max(0,a-250):b+250],re.I)
            out.append({'matched':m.group(0),'value':m.groupdict().get('value'),'unit':'%' if m.groupdict().get('value') else None,'baseline':m.groupdict().get('baseline'),'target_year':m.groupdict().get('year'),'phrase':m.groupdict().get('phrase'),'sentence':sentence.group(0) if sentence else m.group(0)})
    return out

def rules_stage(c,force=False,limit=None,only=None):
    import yaml
    cfg=yaml.safe_load((Path(__file__).parent/'dictionaries.yaml').read_text(encoding='utf-8')); n=0
    hazard_patterns={k:[re.compile(r'\b(?:'+x+r')\b',re.I) for x in v] for k,v in cfg['hazards'].items()}
    finance_patterns=[re.compile(r'\b'+re.escape(x)+r'\b',re.I) for x in cfg['finance']]
    governance_patterns=[re.compile(x,re.I) for x in cfg['governance']]
    implementation_patterns=[re.compile(r'\b'+x+r'\b',re.I) for x in cfg['implementation']]
    track_patterns=[re.compile(x,re.I) for x in cfg['track_record']]
    mitigation_patterns=[re.compile(r'\b'+re.escape(x)+r'\b',re.I) for x in cfg['mitigation']]
    adaptation_patterns=[re.compile(r'\b'+re.escape(x)+r'\b',re.I) for x in cfg['adaptation']]
    target_patterns=_target_patterns(); money_pattern=re.compile(cfg['money_pattern'],re.I)
    action_pattern=re.compile(cfg['action_table_pattern']); body_pattern=re.compile(cfg['governance_body_pattern']); monitoring_pattern=re.compile(cfg['monitoring_pattern'],re.I)
    cip_pattern=re.compile(cfg['cip_pattern'],re.I); bond_pattern=re.compile(cfg['bond_pattern'],re.I); grant_pattern=re.compile(cfg['grant_pattern'],re.I)
    for plan in selected(c,only,limit):
        pid=plan['plan_id']
        if done(c,pid,'rules') and not force: continue
        mark(c,pid,'rules','running')
        try:
            pgs=c.execute('SELECT page_no,text FROM pages WHERE plan_id=? ORDER BY page_no',(pid,)).fetchall(); hz={}; targets=[]; money=0; gov=[]; hits=[]; relevance={}; mit=ada=0;
            for pg in pgs:
                t=pg['text']; found={}
                for name,pats in hazard_patterns.items():
                    cnt=sum(len(p.findall(t)) for p in pats)
                    if cnt: hz.setdefault(name,[]).append({'page':pg['page_no'],'count':cnt}); found[name]=cnt
                tg=target_hits(t,target_patterns); targets.extend([dict(x,page=pg['page_no']) for x in tg]); money_matches=list(money_pattern.finditer(t)); money+=len(money_matches)
                fin=sum(len(p.findall(t)) for p in finance_patterns); gv=[p.pattern for p in governance_patterns if p.search(t)]; impl=[p.pattern for p in implementation_patterns if p.search(t)]; tr=[p.pattern for p in track_patterns if p.search(t)]
                body={'hazards':found,'targets':tg,'money':[{'match':m.group(),'context':t[max(0,m.start()-125):m.end()+125]} for m in money_matches],'finance_count':fin,'governance':gv,'implementation':impl,'track_record':tr}
                score=sum(found.values())*3+len(tg)*4+len(money_matches)+fin+len(gv)*2+len(impl)+len(tr); relevance[pg['page_no']]=score
                action=bool(action_pattern.search(t))
                c.execute('INSERT OR REPLACE INTO page_hits VALUES(?,?,?,?,?)',(pid,pg['page_no'],json.dumps(body),score,int(action)))
            alltext=' '.join(x['text'] for x in pgs); targets=target_hits(alltext,target_patterns)
            govb=list(dict.fromkeys(body_pattern.findall(alltext)))
            mit=sum(len(p.findall(alltext)) for p in mitigation_patterns); ada=sum(len(p.findall(alltext)) for p in adaptation_patterns)
            feats={'hazard_counts':{k:sum(x['count'] for x in v) for k,v in hz.items()},'hazard_first_pages':{k:min(x['page'] for x in v) for k,v in hz.items()},'has_quantified_target':bool(targets),'target_years':sorted({x['target_year'] for x in targets if x.get('target_year')}),'target_pcts':[x.get('value') for x in targets if x.get('value')],'net_zero_year':next((x['target_year'] for x in targets if x.get('phrase')) ,None),'n_money_mentions':money,'mentions_CIP':bool(cip_pattern.search(alltext)),'mentions_bond':bool(bond_pattern.search(alltext)),'mentions_grant':bool(grant_pattern.search(alltext)),'has_monitoring_language':bool(monitoring_pattern.search(alltext)),'n_governance_bodies':len(govb),'governance_bodies':govb,'mitigation_term_count':mit,'adaptation_term_count':ada,'orientation_rule_guess':'Combined' if mit and ada else ('Mitigation' if mit else ('Adaptation' if ada else 'Unclear'))}
            c.execute('INSERT OR REPLACE INTO rule_features VALUES(?,?)',(pid,json.dumps(feats))); mark(c,pid,'rules','done'); n+=1
        except Exception as e: error(c,pid,'rules','',e)
    return n
