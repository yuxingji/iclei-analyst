# Local CAP PDF extraction

This package extracts climate-plan PDF text, deterministic rule features, records from Ollama or DeepSeek Flash, validation evidence, pilot evaluation, and Excel/Parquet outputs. The workbook is the field source of truth and must be named `CAP_PDF_Structured_Extraction_Template_v2_1.xlsx` in the project root. Inputs are expected under `outputs/iclei_cap/`.

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

Use `--only PLAN_ID [PLAN_ID ...]`, `--limit N`, and `--force` to select or rerun work. If inputs are not under the defaults, pass `--pdf-dir`, `--atlas`, `--template`, and/or `--output-dir` to each invocation. SQLite checkpoints live at `outputs/cap_extract/cap.sqlite`; each stage resumes from per-plan status. Ollama is retained for local prompt debugging (`--provider ollama`). Run a reviewed full extraction with `python -m cap_extract llm --provider deepseek --budget-usd 10 --concurrency 8`; resume with the same command. Up to eight chunk requests run concurrently while SQLite writes are handled by the stage thread. `--offpeak-discount 0.5` affects reported cost only, so start the command during the off-peak period manually if desired. Use `python -m cap_extract estimate --provider deepseek` for an offline estimate. The DeepSeek path requires `DEEPSEEK_API_KEY` and never prints it. The Run Info export records provider/model IDs, prompt hashes, run dates, usage totals, and cost.

## Outputs

`outputs/cap_extract/` contains the SQLite database, `eval_report.csv`, `CAP_extraction_output.xlsx`, and Parquet exports. Rule features are emitted without running the LLM. Errors are recorded per plan in the SQLite `errors` table.

Run the parser checks with `python -m unittest discover -s tests`.
