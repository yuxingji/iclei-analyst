"""Compare two saved pilot exports without overwriting the original run."""
from collections import Counter

STATUSES=('Answered','Explicit No','Not Disclosed','Not Applicable','Unclear')


def compare_exports(before,after):
    rows=[]
    pdfs=sorted({a['pdf_id'] for a in before['answers']}|{a['pdf_id'] for a in after['answers']})
    for pdf in ['All PDFs']+pdfs:
        def selected(data):
            return [a for a in data['answers'] if a.get('answer_part',1)==1 and (pdf=='All PDFs' or a['pdf_id']==pdf)]
        a,b=selected(before),selected(after)
        for status in STATUSES:
            rows.append({'pdf_id':pdf,'metric':status,'v1':sum(x['disclosure_status']==status for x in a),
                         'v2':sum(x['disclosure_status']==status for x in b)})
        for completeness in ('Complete','Partial'):
            rows.append({'pdf_id':pdf,'metric':'Answered / '+completeness,'v1':'Not recorded',
                         'v2':sum(x['disclosure_status']=='Answered' and x.get('answer_completeness')==completeness for x in b)})
        before_flags=Counter(x['flag_type'] for x in before['flags'] if pdf=='All PDFs' or x['pdf_id']==pdf)
        after_flags=Counter(x['flag_type'] for x in after['flags'] if pdf=='All PDFs' or x['pdf_id']==pdf)
        for flag in sorted(set(before_flags)|set(after_flags)):
            rows.append({'pdf_id':pdf,'metric':'flag: '+flag,'v1':before_flags[flag],'v2':after_flags[flag]})
    return rows


def changed_answers(before,after):
    old={(a['pdf_id'],a['question_id']):a for a in before['answers'] if a.get('answer_part',1)==1}
    new={(a['pdf_id'],a['question_id']):a for a in after['answers'] if a.get('answer_part',1)==1}
    return [{'pdf_id':key[0],'question_id':key[1],
             'v1_status':old[key]['disclosure_status'],'v2_status':new[key]['disclosure_status'],
             'v1_answer':old[key]['consolidated_answer'],'v2_answer':new[key]['consolidated_answer'],
             'v2_pages':new[key].get('evidence_page','')}
            for key in sorted(old.keys()&new.keys()) if old[key]['disclosure_status']!=new[key]['disclosure_status']]


def selected_comparisons(before,after,manifest):
    wanted={'CA_Obispo_2023_f8e9d6','TX_Austin_2024_b9aab7','CO_County_2008_0c761c'}
    questions={'1.1','1.2','2.5','3.2','7.1.1','9.1.1','10.1'};rows=[]
    for plan in manifest:
        if plan['plan_id'] not in wanted:continue
        for qid in sorted(questions):
            def parts(data):
                return sorted((a for a in data['answers'] if a['pdf_id']==plan['pdf_id'] and a['question_id']==qid),key=lambda x:x.get('answer_part',1))
            old,new=parts(before),parts(after)
            for index in range(max(len(old),len(new))):
                a=old[index] if index<len(old) else {};b=new[index] if index<len(new) else {}
                rows.append({'pdf_id':plan['pdf_id'],'question_id':qid,'answer_part':index+1,
                             'v1_answer':a.get('consolidated_answer',''),'v2_answer':b.get('consolidated_answer',''),
                             'v2_status':b.get('disclosure_status',''),'v2_pages':b.get('evidence_page','')})
    return rows
