import fs from 'node:fs/promises';
import { FileBlob, SpreadsheetFile } from '@oai/artifact-tool';

const path='outputs/cdp/cdp_2026_codebook.xlsx';
const prepared=JSON.parse(await fs.readFile('outputs/cdp/consolidated_question_cards_prepared.json','utf8'));
const wb=await SpreadsheetFile.importXlsx(await FileBlob.load(path));
const cb=wb.worksheets.getItem('Codebook');
const h=cb.getRangeByIndexes(0,0,1,30).values[0];
const scope=h.indexOf('suggested_scope'), judgement=h.indexOf('answerability_judgement'), rationale=h.indexOf('answerability_rationale');
if ([scope,judgement,rationale].some(i=>i<0)) throw new Error('Existing codebook headers not found');
const count=493;
const existing=cb.getRangeByIndexes(1,0,count,30).values;
const cards=new Map(prepared.cards.map(c=>[c.question_id,c]));
cb.getRangeByIndexes(1,scope,count,1).values=existing.map(()=>['Core']);
const extraHeaders=['answerability_note','cdp_2025_question_id_proposed','cdp_2025_match_score','cdp_2025_match_method','document_evident_rule'];
cb.getRangeByIndexes(0,30,1,extraHeaders.length).values=[extraHeaders];
cb.getRangeByIndexes(1,30,count,extraHeaders.length).values=existing.map(r=>{
  const c=cards.get(r[0]); if(!c) throw new Error(`Question missing: ${r[0]}`);
  return [`${r[judgement]||''}: ${r[rationale]||''}`,c.cdp_2025_question_id_proposed,c.cdp_2025_match_score,c.cdp_2025_match_method,c.document_evident_rule||''];
});
const ab=wb.worksheets.getItem('Answerability');
const ah=ab.getRangeByIndexes(0,0,1,10).values[0];
const aq=ah.indexOf('question_id'), as=ah.indexOf('suggested_scope'), af=ah.indexOf('researcher_final_scope');
const aj=ah.indexOf('answerability_judgement'), ar=ah.indexOf('rationale');
if([aq,as,af,aj,ar].some(i=>i<0)) throw new Error('Existing Answerability headers not found');
const av=ab.getRangeByIndexes(1,0,62,10).values;
ab.getRangeByIndexes(1,as,62,1).values=av.map(()=>['Core']);
ab.getRangeByIndexes(1,af,62,1).values=av.map(()=>['Core']);
ab.getRangeByIndexes(0,10,1,1).values=[['answerability_note']];
ab.getRangeByIndexes(1,10,62,1).values=av.map(r=>[`${r[aj]||''}: ${r[ar]||''}`]);

function add(name,headers,records) {
  let s;
  try { s=wb.worksheets.getItem(name); } catch { s=null; }
  if(!s) s=wb.worksheets.add(name);
  else s.getRangeByIndexes(0,0,Math.max(records.length+1,250),headers.length).clear({applyTo:'contents'});
  const rows=[headers,...records.map(r=>headers.map(h=>r[h]??''))];
  for(let i=0;i<rows.length;i+=100){const b=rows.slice(i,i+100);s.getRangeByIndexes(i,0,b.length,headers.length).values=b;}
  s.freezePanes.freezeRows(1);s.getRangeByIndexes(0,0,1,headers.length).format.font.bold=true;
  s.getRangeByIndexes(0,0,rows.length,headers.length).format.columnWidth=22;
  return s;
}
const cardHeaders=['question_id','module','question_text','is_multi_item','item_unit','fields_in_order','option_vocabulary','key_guidance','answer_template','not_applicable_rule','inference_basis','document_evident_rule','cdp_2025_question_id_proposed','cdp_2025_match_score','cdp_2025_match_method'];
add('Question Cards',cardHeaders,prepared.cards);
const crossHeaders=['cdp_2025_question_id','cdp_2025_question_text','cdp_2026_question_id','cdp_2026_question_text','match_score','match_method','review_status'];
add('Crosswalk 2025',crossHeaders,prepared.crosswalk);
const spec=wb.worksheets.getItem('Output Spec');spec.getRange('A1:B30').clear({applyTo:'contents'});
const specRows=[['item','specification'],
 ['Main Response','One row per pdf_id x question_id x answer_part; answer blank if Not Disclosed.'],
 ['Fields','pdf_id, plan_id, municipality, state, gov_type, source_file, disclosure_year, question_id, question_text, consolidated_answer, disclosure_status, evidence_page, answer_part.'],
 ['Disclosure year','Use plan_publish_year from the CAP results.'],
 ['Answer format','Use concise natural answers with supported items separated by semicolons; omit missing fields.'],
 ['Attribution','Keep cost, target, owner, funding and status attached to their own item.'],
 ['Supporting Evidence','One or more verbatim evidence rows per item: pdf_id, question_id, item_label, evidence_page, evidence_excerpt, confidence, evidence_verified, page_corrected.'],
 ['Review Flags','pdf_id, question_id, flag_type, reason, consolidated_answer, disclosure_status.'],
 ['Disclosure Status','Answered / Explicit No / Not Disclosed / Not Applicable / Unclear. Do not infer No or zero from absence.'],
 ['Source restriction','Answers and evidence come only from the CAP PDF. The 2025 workbook is used only for question crosswalk and pilot municipality selection.'],
 ['Crosswalk','Proposed question-text similarity only; researcher confirmation required.'],
 ['Run Info','Prompt version/hash, model, calls, tokens, peak/off-peak cost and selected pages.'],
 ['Scope','All 62 questionnaire questions are Core.'],
 ['Codebook provenance','Question wording and guidance come from the 2026 questionnaire PDF and the existing codebook.']];
spec.getRangeByIndexes(0,0,specRows.length,2).values=specRows;
spec.getRangeByIndexes(0,0,specRows.length,2).format.wrapText=true;
spec.getRangeByIndexes(1,0,specRows.length-1,2).format.rowHeight=55;
const result=await SpreadsheetFile.exportXlsx(wb);await result.save(path);
console.log('Updated existing codebook; cards='+prepared.cards.length+' crosswalk_rows='+prepared.crosswalk.length);
