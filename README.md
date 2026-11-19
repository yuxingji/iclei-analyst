# Local CAP PDF extraction

This package extracts climate-plan PDF text, deterministic rule features, records from Ollama or DeepSeek Flash, validation evidence, pilot evaluation, and Excel/Parquet outputs. The active extraction schema is v2.3 in `cap_extract/schema_v2_3.yaml`: it restores the reviewed v2.2 Plan/Risk/Action fields and risk rules, then adds action orientation and structured cost disclosure. The workbook contains Plan Level, separate Risk Level, individual Action Detail, and an aggregated Action Level with at most one Mitigation and one Adaptation row per plan. Older schema artifacts remain available by version. Inputs are expected under `outputs/iclei_cap/`.

## Setup

Python 3.11+ is required. Install packages with:

```powershell
python -m pip install -r requirements.txt
ollama pull qwen2.5:7b-instruct
python -m pip install openai
```

For DeepSeek, set the key for the current PowerShell session with `$env:DEEPSEEK_API_KEY="..."` or persist it with `setx DEEPSEEK_API_KEY "..."` (a new shell is needed after `setx`). The API model id for V4.1 Flash is `deepseek-flash`, as stated in the official [V4.1-Flash release notes](https://api-docs.deepseek.com/news/news260910/). The official [JSON Output guide](https://api-docs.deepseek.com/guides/json_mode/) demonstrates `model="deepseek-flash"` with `response_format={"type":"json_object"}`. The [Thinking Mode guide](https://api-docs.deepseek.com/guides/thinking_mode/) documents `reasoning_effort="none"` to disable thinking; this pipeline defaults to `none`, configurable with `--effort` (`low`, `high`, `max` re-enable thinking). The [Chat Completions API reference](https://api-docs.deepseek.com/api/create-chat-completion/) documents cached/uncached input, completion, and reasoning-token usage fields. Peak rates used for budget enforcement and the separate off-peak reporting discount are listed on DeepSeek's official [Models & Pricing page](https://api-docs.deepseek.com/quick_start/pricing/).

Install `ocrmypdf` separately to OCR image-only PDFs. Without it, plans averaging fewer than 200 extracted characters per page are marked `needs_ocr` and skipped safely.

## Recommended run order

```powershell
python -m cap_extract inventory
python -m cap_extract text
python -m cap_extract rules
python -m cap_extract llm --provider ollama --limit 7
python -m cap_extract validate
python -m cap_extract eval
# Tune prompts/dictionaries, then run all plans:
python -m cap_extract llm
python -m cap_extract validate
python -m cap_extract export
```

Use `--only PLAN_ID [PLAN_ID ...]`, `--limit N`, and `--force` to select or rerun work. If inputs are not under the defaults, pass `--pdf-dir`, `--atlas`, `--template`, and/or `--output-dir` to each invocation. SQLite checkpoints live at `outputs/cap_extract/cap.sqlite`; each stage resumes from per-plan status and schema version. Ollama is retained for local prompt debugging (`--provider ollama`). Run a reviewed full extraction with `python -m cap_extract llm --provider deepseek --effort none --budget-usd 25 --concurrency 8`; resume with the same command and no `--force`. Up to eight plans run concurrently, each processing chunks serially, with SQLite writes serialized through one lock. `--offpeak-discount 0.5` affects reported cost only, so start the command during the off-peak period manually if desired. Use `python -m cap_extract estimate --provider deepseek` for an offline estimate. The DeepSeek path requires `DEEPSEEK_API_KEY` and never prints it. The Run Info export records provider/model IDs, per-task prompt hashes, run dates, usage totals, and cost.

## Outputs

`outputs/cap_extract/` contains the SQLite database, `eval_report.csv`, `CAP_extraction_output.xlsx`, and Parquet exports. Rule features are flattened to one column per feature. Omitted text fields are normalized to `Not Disclosed`; numeric missing values remain null in Parquet to preserve numeric columns. Errors are recorded per plan in the SQLite `errors` table.

Run the parser checks with `python -m unittest discover -s tests`.

## CDP consolidated pilot

The CDP workflow is separate from the CAP extraction and uses `outputs/cdp/cdp.sqlite`.
The 2026 questionnaire supplies the question wording, definitions, guidance and option
vocabulary. The 2025 workbook is used only to propose a question crosswalk and select
pilot municipalities; its answers are not supplied to the model. Generated question
cards and the ten-document manifest are saved under `outputs/cdp/`.

`cap_extract.cdp_consolidated_run` takes a `--budget-usd` cap and a
`--max-calls-per-plan` cap and skips completed groups on resume. It reads CAP page
text without modifying the CAP database. Version 2 sends every indexed page, in
consecutive chunks when needed, for every question group. Pages without usable text
are flagged, so a document with unreadable pages cannot be treated as fully searched.
Check the evidence and review flags before using pilot results. Output-capped groups
are split by question; a capped single question is left incomplete for review. Invalid
JSON is never repaired by guessing. The peak cost and the separate
off-peak reporting estimate are recorded in Run Info; unknown charges can be reserved
against the budget after an interrupted request.

The `cdp_tools/` scripts build the XLSX files and report using the Codex bundled
`@oai/artifact-tool` runtime. The Python extraction/export modules and test suite live
under `cap_extract/` and `tests/`. The pilot is not a full-corpus run.

The full-text model input is placed before the per-group question cards to allow
prefix caching, but actual cache hits and costs must be measured from provider usage.
The current full-document byte threshold is deliberately conservative relative to
the 1,048,576-token context published in the official [model-list example](https://api-docs.deepseek.com/api/list-models/).
The automated quote and numeric checks identify potential problems; they do not prove
that a quotation supports a claim, so manual review remains necessary.

The full CDP job freezes the PDF/text manifest, verifies and reuses the approved pilot
by prompt hash, and checkpoints each group in the isolated CDP database. It keeps a
single SQLite writer and stops on insufficient balance or its cumulative USD budget.
The current run can be resumed with the same command and no force flag:

```powershell
$env:PYTHONPATH='work\cap_extract_runtime'
python -u -m cap_extract.cdp_full_job --budget-usd 56 --max-calls-per-plan 150 --concurrency 8
```

The full job uses conservative time-aware prices for budget accounting; requests
crossing a peak period are charged at peak, and Chinese public holidays are treated
as weekdays for safety. Unknown in-flight charges are reserved until reviewed.
The USD budget is cumulative for the run; raising it requires a deliberate new value.
Logs are `outputs/cdp/consolidated_full_job.log` and
`outputs/cdp/consolidated_full_job_error.log`. The job writes a clearly marked
`PARTIAL` workbook if it stops before completion, plus Parquet and a JSON summary.
