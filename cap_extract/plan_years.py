"""Extract defensible plan publication and active-period years."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pydantic import BaseModel

from .core import DB, schema_config
from .providers import ProviderCallError, RequestBudget, get_provider

YEAR = r"(?:19|20)\d{2}"
YEAR_RANGE = re.compile(rf"(?<!\d)({YEAR})\s*(?:-|–|—|to)\s*({YEAR})(?!\d)", re.I)
MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
PUBLICATION_DATE = re.compile(
    rf"\b(?:publication\s+date|date\s+published|issue\s+date|published|issued|released)\b\s*(?::|on\b|in\b)?\s*(?:{MONTH}\s+)?({YEAR})\b",
    re.I,
)


class PlanYearItem(BaseModel):
    publication_year: int | None = None
    publication_quote: str | None = None
    publication_page: str | None = None
    active_year: str | None = None
    active_quote: str | None = None
    active_page: str | None = None


class PlanYearResponse(BaseModel):
    records: list[PlanYearItem] = []


SYSTEM = (
    "Extract only plan publication/issue year and the plan's explicit coverage period. "
    "Publication year must come from a stated PDF publication, issue, or release date; "
    "an adoption date is not a publication date. Active year is only the explicit plan "
    "period or planning horizon. Do not infer it from emissions targets, net-zero dates, "
    "or other target years. Accept YYYY-YYYY, YYYY-YYYY text, or -YYYY only when the "
    "plan explicitly states an end year without a start. Return exact short quotes and "
    "the supplied [PAGE n]. If absent, omit the key. Return one object in records."
)


def _norm_dashes(text: str) -> str:
    return str(text or "").replace("–", "-").replace("—", "-").replace("−", "-")


def _page_year_range(text: str):
    m = YEAR_RANGE.search(_norm_dashes(text))
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}", m.group(1), m.group(2)


def _title_range(plan, pages):
    title_file = f"{plan['plan_title'] or ''} {plan['source_file'] or ''}"
    candidate = _page_year_range(title_file)
    if not candidate:
        return None
    value, start, end = candidate
    for page in pages:
        text = _norm_dashes(page['text'] or "")
        if int(start)>int(end):
            continue
        if any(r[0]==value for r in (_page_year_range(line) for line in text.splitlines()) if r):
            lines = [line.strip() for line in text.splitlines() if (r:=_page_year_range(line)) and r[0]==value]
            quote = next((line[:250] for line in lines if line), f"{plan['plan_title']} ({value})")
            return {"active_year": value, "active_quote": quote, "active_page": str(page['page_no'])}
    return None


def _year_headings(pages):
    selected = {int(p['page_no']): p['text'] or "" for p in pages[:4]}
    for p in pages:
        text = p['text'] or ""
        for line in text.splitlines():
            line0 = line.strip()
            if YEAR_RANGE.search(_norm_dashes(line0)) and (
                len(line0) <= 180 or line0.isupper() or line0.startswith("#")
            ):
                selected[int(p['page_no'])] = text
                break
    return selected


def _publication_from_regex(pages):
    for page in pages[:4]:
        for line in (page['text'] or "").splitlines():
            match = PUBLICATION_DATE.search(line)
            if match:
                before=line[max(0,match.start()-400):match.start()]
                if re.search(r"\b(?:first|original|former|prior|previous|earlier|legacy|old|amended|updated|revised|version|inventory|guidance|governor|state|regional|district|colorado)\b[^.!?\n]{0,400}$",before,re.I):
                    continue
                return int(match.group(1)), line.strip()[:250] or match.group(0), str(page['page_no'])
    return None, None, None


def _year(value):
    m = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", str(value or ""))
    return int(m.group(0)) if m else None


def _valid_publication_quote(year, quote):
    quote=str(quote or '')
    match = PUBLICATION_DATE.search(quote)
    if match and re.search(r"\b(?:first|original|former|prior|previous|earlier|legacy|old|amended|updated|revised|version|inventory|guidance|governor|state|regional|district|colorado)\b[^.!?\n]{0,400}$",quote[max(0,match.start()-400):match.start()],re.I):
        return False
    return bool(match and int(match.group(1)) == int(year))


def _cost_projection(input_tokens: int, output_tokens: int = 180) -> float:
    # Conservative peak-rate estimate: assume uncached input.
    return (input_tokens * 0.30 + output_tokens * 1.20) / 1_000_000


def run_plan_year_extraction(c, *, provider_name="deepseek", model="deepseek-flash", effort="none", budget_usd=2.0, concurrency=8):
    """Persist plan-year fields and usage. Returns run statistics and plan rows."""
    c.execute("CREATE TABLE IF NOT EXISTS plan_year_results(plan_id TEXT PRIMARY KEY,payload TEXT NOT NULL,method TEXT NOT NULL,prompt_hash TEXT,updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    c.commit()
    plans = c.execute("SELECT * FROM plans ORDER BY plan_id").fetchall()
    plans = [dict(r) for r in plans]
    meta = {
        r['plan_id']: json.loads(r['payload'])
        for r in c.execute("SELECT plan_id,payload FROM exports_v23 WHERE table_name='Plan Level'")
    }
    tasks = []
    resolved = {}
    usable = 0
    all_pages = {}
    for plan in plans:
        pages = [dict(r) for r in c.execute("SELECT page_no,text,n_chars FROM pages WHERE plan_id=? ORDER BY page_no", (plan['plan_id'],)) if r['text']]
        all_pages[plan['plan_id']] = pages
        if not pages:
            existing = c.execute("SELECT payload FROM plan_year_results WHERE plan_id=?", (plan['plan_id'],)).fetchone()
            if existing:
                resolved[plan['plan_id']] = json.loads(existing[0])
            else:
                resolved[plan['plan_id']] = _fallback_values(plan, meta.get(plan['plan_id'], {}), None)
            continue
        usable += 1
        regex_active = _title_range(plan, pages)
        pub_year, pub_quote, pub_page = _publication_from_regex(pages)
        if regex_active:
            result = {**regex_active, "publication_year": pub_year, "publication_quote": pub_quote, "publication_page": pub_page}
            resolved[plan['plan_id']] = _fallback_values(plan, meta.get(plan['plan_id'], {}), result)
            c.execute("INSERT OR REPLACE INTO plan_year_results(plan_id,payload,method,prompt_hash,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)", (plan['plan_id'], json.dumps(resolved[plan['plan_id']], ensure_ascii=False), 'title_regex', None))
            continue
        existing = c.execute("SELECT payload FROM plan_year_results WHERE plan_id=?", (plan['plan_id'],)).fetchone()
        if existing:
            resolved[plan['plan_id']] = json.loads(existing[0])
            continue
        selected = _year_headings(pages)
        page_block = "\n\n".join(f"[PAGE {n}]\n{text}" for n, text in sorted(selected.items()))
        user = f"Plan title: {plan['plan_title'] or ''}\nSource file: {plan['source_file'] or ''}\nICLEI metadata plan_year: {plan['plan_year'] or ''}\n\nSource pages:\n{page_block}"
        tasks.append((plan, pages, user, pub_year, pub_quote, pub_page))
    c.commit()

    schema = PlanYearResponse.model_json_schema()
    prompt_hash = hashlib.sha256(SYSTEM.encode('utf-8')).hexdigest()
    schema_hash = hashlib.sha256(json.dumps(schema, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    run_id = uuid.uuid4().hex
    if not tasks:
        return {"run_id": None, "plans_with_text": usable, "llm_calls_expected": 0, "llm_calls": 0, "cost_usd": 0.0, "prompt_hash": prompt_hash, "rows": resolved}
    api = get_provider(provider_name, model, max_tokens=512, reasoning_effort=effort)
    c.execute("INSERT INTO llm_runs(run_id,provider,requested_model,started_at,status,schema_hash) VALUES(?,?,?,?,?,?)", (run_id, provider_name, model, time.strftime('%Y-%m-%d %H:%M:%S'), 'running', schema_hash))
    c.execute("INSERT OR REPLACE INTO llm_prompt_versions(run_id,task,prompt_hash) VALUES(?,?,?)", (run_id, 'plan_years', prompt_hash))
    c.commit()

    # Estimate each call using exact serialized input plus the fixed system/schema text.
    system = SYSTEM + "\n\nJSON Schema:\n" + json.dumps(schema, ensure_ascii=False, sort_keys=True)
    inputs = [(p, pages, user, pub_year, pub_quote, pub_page, (len(system)+len(user)+2)//3) for p,pages,user,pub_year,pub_quote,pub_page in tasks]
    expected_cost = sum(_cost_projection(t[-1], 180) for t in inputs)
    budget = RequestBudget(10_000, 0.0, budget_usd, 0.002)
    lock = threading.Lock()
    totals = {"calls": 0, "retries": 0, "input_tokens": 0, "cached_input": 0, "output_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0, "errors": [], "balance_stop": None}
    results = {}

    def work(task):
        plan, pages, user, pub_year, pub_quote, pub_page, estimated_tokens = task
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        raw = ""
        usage = {}
        error = None
        try:
            parsed, usage, raw = api.extract('plan_years', messages, PlanYearResponse, budget, _cost_projection(estimated_tokens, 180))
            if parsed is None:
                error = "budget limit reached" if usage.get('budget_reached') else "provider returned no parsed year result"
            else:
                if isinstance(parsed, dict):
                    got = (parsed.get('records') or [{}])[0]
                else:
                    got = parsed.records[0].model_dump(exclude_none=True) if parsed.records else {}
                result = {"publication_year": pub_year or got.get('publication_year'), "publication_quote": pub_quote or got.get('publication_quote'), "publication_page": pub_page or got.get('publication_page'), "active_year": got.get('active_year'), "active_quote": got.get('active_quote'), "active_page": got.get('active_page')}
                source_text = {str(p['page_no']): p['text'] or '' for p in pages}
                if result['publication_year']:
                    pub_page0 = str(result.get('publication_page') or '')
                    quote0 = str(result.get('publication_quote') or '')
                    if not (1900 <= int(result['publication_year']) <= 2100 and quote0 and quote0 in source_text.get(pub_page0, '') and _valid_publication_quote(result['publication_year'],quote0)):
                        result.update({'publication_year': None, 'publication_quote': None, 'publication_page': None})
                active = str(result.get('active_year') or '')
                active_match = re.fullmatch(r"(?:(?:19|20)\d{2}-(?:(?:19|20)\d{2})|-(?:19|20)\d{2})", active)
                active_page0 = str(result.get('active_page') or '')
                active_quote0 = str(result.get('active_quote') or '')
                if not (active_match and active_quote0 and active_quote0 in source_text.get(active_page0, '')):
                    result.update({'active_year': None, 'active_quote': None, 'active_page': None})
                if not result['publication_year']:
                    result['publication_year'], result['plan_publish_year_source'] = _fallback_publish(plan, meta.get(plan['plan_id'], {}))
                else:
                    result['plan_publish_year_source'] = 'PDF publication date'
                result.setdefault('plan_publish_year_source', '')
                result['publication_year_source_quote'] = result.get('publication_quote') or ''
                result['publication_year_source_page'] = result.get('publication_page') or ''
                if not result.get('active_year'):
                    result['active_year'] = 'Not Disclosed'
                result['active_quote'] = result.get('active_quote') or ''
                result['active_page'] = result.get('active_page') or ''
                return plan['plan_id'], result, usage, raw, None
        except ProviderCallError as exc:
            usage = exc.usage or {}
            raw = exc.raw_text or ''
            error = str(exc)
            if exc.stop_reason:
                budget.halt(exc.stop_reason)
                with lock:
                    totals['balance_stop'] = exc.stop_reason
        except Exception as exc:
            error = str(exc)
        result = _fallback_values(plan, meta.get(plan['plan_id'], {}), None)
        result.update({'active_year': 'Not Disclosed', 'active_quote': '', 'active_page': ''})
        return plan['plan_id'], result, usage, raw, error

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(work, task) for task in inputs]
        for future in as_completed(futures):
            pid, result, usage, raw, err = future.result()
            results[pid] = result
            cost = float(usage.get('cost_usd') or 0)
            calls = int(usage.get('api_calls') or 0)
            with lock:
                totals['calls'] += calls
                totals['retries'] += int(usage.get('retries') or 0)
                totals['input_tokens'] += int(usage.get('input_tokens') or 0)
                totals['cached_input'] += int(usage.get('cached_input_tokens') or 0)
                totals['output_tokens'] += int(usage.get('output_tokens') or 0)
                totals['reasoning_tokens'] += int(usage.get('reasoning_tokens') or 0)
                totals['cost_usd'] += cost
                if err: totals['errors'].append((pid, err))
            page = result.get('active_page') or result.get('publication_year_source_page') or ''
            c.execute("INSERT OR REPLACE INTO plan_year_results(plan_id,payload,method,prompt_hash,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)", (pid, json.dumps(result, ensure_ascii=False), 'llm' if not err else 'llm_error_fallback', prompt_hash))
            c.execute("INSERT INTO llm_raw(plan_id,task,model,prompt_hash,created_at,duration,response,error,options,run_id,provider,actual_model,input_cached,input_uncached,output_tokens,cost_usd,retry_count,api_calls,reasoning_tokens,reasoning_cost_usd) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (pid, 'plan_years', model, prompt_hash, time.strftime('%Y-%m-%d %H:%M:%S'), 0, raw, err, json.dumps({'effort':effort,'max_tokens':512,'source_page':page}), run_id, provider_name, usage.get('model_id'), usage.get('cached_input_tokens',0), max(0,int(usage.get('input_tokens',0))-int(usage.get('cached_input_tokens',0))), usage.get('output_tokens',0), cost, usage.get('retries',0), calls, usage.get('reasoning_tokens',0), usage.get('reasoning_cost_usd',0)))
            c.commit()
    resolved.update(results)
    status = 'budget' if totals['cost_usd'] >= budget_usd or budget.budget_reached else 'done'
    if totals['balance_stop']: status = 'budget'
    c.execute("UPDATE llm_runs SET ended_at=?,actual_model=?,input_cached=?,input_uncached=?,output_tokens=?,reasoning_tokens=?,reasoning_cost_usd=?,cost_usd=?,cost_discounted_usd=?,api_calls=?,retries=?,status=? WHERE run_id=?", (time.strftime('%Y-%m-%d %H:%M:%S'), model, totals['cached_input'], max(0,totals['input_tokens']-totals['cached_input']), totals['output_tokens'], totals['reasoning_tokens'], totals['reasoning_tokens']*1.2/1_000_000, totals['cost_usd'], totals['cost_usd']*.5, totals['calls'], totals['retries'], status, run_id))
    c.commit()
    return {"run_id":run_id,"plans_with_text":usable,"llm_calls_expected":len(tasks),"estimated_cost_usd":expected_cost,"llm_calls":totals['calls'],"retries":totals['retries'],"input_tokens":totals['input_tokens'],"cached_input_tokens":totals['cached_input'],"output_tokens":totals['output_tokens'],"reasoning_tokens":totals['reasoning_tokens'],"cost_usd":totals['cost_usd'],"status":status,"balance_stop":totals['balance_stop'],"errors":totals['errors'],"prompt_hash":prompt_hash,"rows":resolved}


def _fallback_publish(plan, plan_row):
    adoption = _year(plan_row.get('adoption_date'))
    if adoption:
        return adoption, 'Adoption date'
    return _year(plan.get('plan_year')), 'ICLEI metadata'


def _fallback_values(plan, plan_row, result):
    result = result or {}
    pub = result.get('publication_year')
    if pub:
        source = 'PDF publication date'
    else:
        pub, source = _fallback_publish(plan, plan_row)
    active = result.get('active_year') or 'Not Disclosed'
    return {
        'plan_publish_year': pub,
        'plan_publish_year_source': source,
        'plan_active_year': active,
        'publication_year_source_quote': result.get('publication_quote') or (plan_row.get('formal_status_verbatim') or '' if source == 'Adoption date' else ''),
        'publication_year_source_page': result.get('publication_page') or (plan_row.get('source_page') or '' if source == 'Adoption date' else ''),
        'active_quote': result.get('active_quote') or '',
        'active_page': result.get('active_page') or '',
    }


def reparse_plan_year_responses(c):
    """Recover validated JSON stored in llm_raw without making API calls."""
    c.execute("CREATE TABLE IF NOT EXISTS plan_year_results(plan_id TEXT PRIMARY KEY,payload TEXT NOT NULL,method TEXT NOT NULL,prompt_hash TEXT,updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    plans = {r['plan_id']: dict(r) for r in c.execute('SELECT * FROM plans')}
    plan_rows = {r['plan_id']: json.loads(r['payload']) for r in c.execute("SELECT plan_id,payload FROM exports_v23 WHERE table_name='Plan Level'")}
    pages_by_plan = {}
    for row in c.execute('SELECT plan_id,page_no,text FROM pages ORDER BY plan_id,page_no'):
        pages_by_plan.setdefault(row['plan_id'], []).append({'page_no': row['page_no'], 'text': row['text'] or ''})
    recovered = 0
    invalid = []
    for raw in c.execute("SELECT plan_id,prompt_hash,response FROM llm_raw WHERE task='plan_years' ORDER BY id"):
        pid = raw['plan_id']
        if pid not in plans:
            continue
        try:
            wrapper = json.loads(raw['response'] or '{}')
            record = (wrapper.get('records') or [{}])[0]
            source_pages = {str(p['page_no']): p['text'] for p in pages_by_plan.get(pid, [])}
            pub_year, pub_quote, pub_page = _publication_from_regex(pages_by_plan.get(pid, []))
            if not pub_year:
                pub_year = record.get('publication_year')
                pub_quote = record.get('publication_quote')
                pub_page = str(record.get('publication_page') or '')
                if not (pub_year and pub_quote and pub_quote in source_pages.get(pub_page, '') and _valid_publication_quote(pub_year,pub_quote)):
                    pub_year = pub_quote = pub_page = None
            active = str(record.get('active_year') or '')
            active_quote = str(record.get('active_quote') or '')
            active_page = str(record.get('active_page') or '')
            valid_active = bool(re.fullmatch(r"(?:(?:19|20)\d{2}-(?:(?:19|20)\d{2})|-(?:19|20)\d{2})", active))
            if not (valid_active and active_quote and active_quote in source_pages.get(active_page, '')):
                active = 'Not Disclosed'; active_quote = ''; active_page = ''
            candidate = {'publication_year': pub_year, 'publication_quote': pub_quote, 'publication_page': pub_page, 'active_year': active, 'active_quote': active_quote, 'active_page': active_page}
            result = _fallback_values(plans[pid], plan_rows.get(pid, {}), candidate)
            c.execute("INSERT OR REPLACE INTO plan_year_results(plan_id,payload,method,prompt_hash,updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP)", (pid, json.dumps(result, ensure_ascii=False), 'llm_raw_reparsed', raw['prompt_hash']))
            recovered += 1
        except Exception as exc:
            invalid.append((pid, str(exc)))
    c.commit()
    return {'recovered': recovered, 'invalid': invalid}


def refresh_plan_publication_fallbacks(c):
    """Apply the specified publication > adoption > ICLEI priority offline."""
    plans = {r['plan_id']: dict(r) for r in c.execute('SELECT * FROM plans')}
    plan_rows = {r['plan_id']: json.loads(r['payload']) for r in c.execute("SELECT plan_id,payload FROM exports_v23 WHERE table_name='Plan Level'")}
    page_map = {}
    for row in c.execute('SELECT plan_id,page_no,text FROM pages ORDER BY plan_id,page_no'):
        page_map.setdefault(row['plan_id'], []).append({'page_no':row['page_no'],'text':row['text'] or ''})
    changed=0
    for row in c.execute('SELECT plan_id,payload FROM plan_year_results').fetchall():
        pid=row['plan_id']; result=json.loads(row['payload']); pages=page_map.get(pid,[])
        pub_year,pub_quote,pub_page=_publication_from_regex(pages)
        if not pub_year:
            candidate_year=result.get('publication_year')
            candidate_quote=str(result.get('publication_quote') or '')
            candidate_page=str(result.get('publication_page') or '')
            page_text=next((p['text'] for p in pages if str(p['page_no'])==candidate_page), '')
            if candidate_year and candidate_quote and candidate_quote in page_text and _valid_publication_quote(candidate_year,candidate_quote):
                pub_year,pub_quote,pub_page=int(candidate_year),candidate_quote,candidate_page
        if pub_year:
            result.update({'plan_publish_year':pub_year,'plan_publish_year_source':'PDF publication date','publication_year_source_quote':pub_quote or '', 'publication_year_source_page':pub_page or ''})
        else:
            pub_year,source=_fallback_publish(plans[pid],plan_rows.get(pid,{}))
            result.update({'plan_publish_year':pub_year,'plan_publish_year_source':source})
            if source=='Adoption date':
                result['publication_year_source_quote']=plan_rows.get(pid,{}).get('formal_status_verbatim') or ''
                result['publication_year_source_page']=plan_rows.get(pid,{}).get('source_page') or ''
            else:
                result['publication_year_source_quote']=''; result['publication_year_source_page']=''
        c.execute("UPDATE plan_year_results SET payload=?,updated_at=CURRENT_TIMESTAMP WHERE plan_id=?",(json.dumps(result,ensure_ascii=False),pid))
        changed+=1
    c.commit()
    return changed
