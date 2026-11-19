import fs from 'node:fs/promises';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {Workbook,SpreadsheetFile} from '@oai/artifact-tool';

if (process.env.CDP_PARQUET_PREFIX) {
  const wb=Workbook.create();
  const python=process.env.CDP_PYTHON||'C:/Users/Administrator/anaconda3/python.exe';
  const prefix=process.env.CDP_PARQUET_PREFIX;
  const safe=v=>v===null||v===undefined?'':typeof v==='string'&&/^[=+\-@]/.test(v)?"'"+v:v;
  const specs=[
    ['Main Response','answers',['pdf_id','plan_id','municipality','state','gov_type','source_file','disclosure_year','question_id','question_text','consolidated_answer','disclosure_status','evidence_page','answer_part','missing_components','answer_completeness','inference_basis']],
    ['Supporting Evidence','evidence',['pdf_id','question_id','item_label','evidence_page','evidence_excerpt','confidence','evidence_verified','page_corrected']],
    ['Review Flags','flags',['pdf_id','question_id','flag_type','reason','consolidated_answer','disclosure_status']],
    ['Codebook','cards',['question_id','module','question_text','fields_in_order','option_vocabulary','key_guidance','answer_template','not_applicable_rule','inference_basis','document_evident_rule','cdp_2025_question_id_proposed']],
    ['Run Info','run_info',['key','value']]
  ];
  const only=process.env.CDP_SHEETS?new Set(process.env.CDP_SHEETS.split(',')):null;
  for (const [name,key,headers] of specs.filter(x=>!only||only.has(x[1]))) {
    const s=wb.worksheets.add(name);s.getRangeByIndexes(0,0,1,headers.length).values=[headers];
    s.freezePanes.freezeRows(1);s.getRangeByIndexes(0,0,1,headers.length).format.font.bold=true;
    s.getRangeByIndexes(0,0,1,headers.length).format.columnWidth=22;
    const child=spawn(python,['-u','cdp_tools/parquet_rows.py',`${prefix}_${key}.parquet`],{stdio:['ignore','pipe','inherit']});
    const done=new Promise((resolve,reject)=>{child.once('error',reject);child.once('close',resolve)});
    const lines=createInterface({input:child.stdout,crlfDelay:Infinity});let batch=[],row=1;
    for await (const line of lines) {
      const record=JSON.parse(line);batch.push(headers.map(h=>safe(record[h])));
      if(batch.length===250){s.getRangeByIndexes(row,0,batch.length,headers.length).values=batch;row+=batch.length;batch=[];}
    }
    if(batch.length){s.getRangeByIndexes(row,0,batch.length,headers.length).values=batch;row+=batch.length;}
    const code=await done;
    if(code!==0)throw new Error(`Parquet reader failed for ${key}: ${code}`);
    console.log(`${name}: ${row-1} rows`);
  }
  wb.recalculate();
  const blob=await SpreadsheetFile.exportXlsx(wb);await blob.save(process.env.CDP_PILOT_XLSX);
  console.log('Workbook exported: '+process.env.CDP_PILOT_XLSX);
} else {
const data=JSON.parse(await fs.readFile(process.env.CDP_PILOT_JSON||'outputs/cdp/consolidated_pilot_export.json','utf8'));
const wb=Workbook.create();
const safe=v=>v===null||v===undefined?'':typeof v==='string'&&/^[=+\-@]/.test(v)?"'"+v:v;
function sheet(name,headers,records) {
  const s=wb.worksheets.add(name);
  const matrix=[headers,...records.map(row=>headers.map(h=>safe(row[h])))];
  for(let i=0;i<matrix.length;i+=250){let part=matrix.slice(i,i+250);s.getRangeByIndexes(i,0,part.length,headers.length).values=part;}
  s.freezePanes.freezeRows(1);s.getRangeByIndexes(0,0,1,headers.length).format.font.bold=true;
  s.getRangeByIndexes(0,0,matrix.length,headers.length).format.columnWidth=22;
  return s;
}
const main=sheet('Main Response',['pdf_id','plan_id','municipality','state','gov_type','source_file','disclosure_year','question_id','question_text','consolidated_answer','disclosure_status','evidence_page','answer_part','missing_components','answer_completeness','inference_basis'],data.answers);
main.getRangeByIndexes(0,8,data.answers.length+1,2).format.columnWidth=55;
const ev=sheet('Supporting Evidence',['pdf_id','question_id','item_label','evidence_page','evidence_excerpt','confidence','evidence_verified','page_corrected'],data.evidence);
ev.getRangeByIndexes(0,4,data.evidence.length+1,1).format.columnWidth=70;
sheet('Review Flags',['pdf_id','question_id','flag_type','reason','consolidated_answer','disclosure_status'],data.flags);
sheet('Codebook',['question_id','module','question_text','fields_in_order','option_vocabulary','key_guidance','answer_template','not_applicable_rule','inference_basis','document_evident_rule','cdp_2025_question_id_proposed'],data.cards);
if(data.comparison)sheet('v1 vs v2',['pdf_id','metric','v1','v2'],data.comparison);
if(data.changed_answers)sheet('Changed answers',['pdf_id','question_id','v1_status','v2_status','v1_answer','v2_answer','v2_pages'],data.changed_answers);
if(data.selected_comparisons)sheet('Selected comparisons',['pdf_id','question_id','answer_part','v1_answer','v2_answer','v2_status','v2_pages'],data.selected_comparisons);
const runinfo=[
 {key:'run_id',value:data.run_id},{key:'prompt_version',value:data.prompt_version||'cdp-consolidated-v1'},
 {key:'run_status',value:data.run_status||'unknown'},
 {key:'full_guidance',value:'cdp_2026_codebook.xlsx'},
 ...Object.entries(data.run_info).map(([key,value])=>({key,value})),
 {key:'cost_offpeak_reporting_only',value:data.run_info.cost_offpeak??data.run_info.cost*.5},
 ...data.calls.map(x=>({key:`${x.pdf_id} / ${x.group_id}`,value:`pages=${x.pages}; prompt_hash=${x.prompt_hash}; calls=${x.api_calls}; retries=${x.retries}; cached=${x.input_cached}; uncached=${x.input_uncached}; output=${x.output_tokens}; cost_usd=${x.cost_usd}; model=${x.actual_model||''}`}))
];
const info=sheet('Run Info',['key','value'],runinfo);
info.getRangeByIndexes(0,1,runinfo.length+1,1).format.columnWidth=100;
const blob=await SpreadsheetFile.exportXlsx(wb);await blob.save(process.env.CDP_PILOT_XLSX||'outputs/cdp/CDP_consolidated_pilot.xlsx');
console.log(`Pilot workbook: ${data.answers.length} answer rows, ${data.evidence.length} evidence rows, ${data.flags.length} flags`);
}
