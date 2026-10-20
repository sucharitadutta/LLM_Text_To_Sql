# Marketing text-to-SQL assistant

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Auth
export ANTHROPIC_API_KEY=...
gcloud auth application-default login          # dev
# prod: attach a service account with ONLY:
#   roles/bigquery.jobUser (project) + roles/bigquery.dataViewer (marketing_mart dataset)

# Optional knobs
export T2S_MODEL=claude-sonnet-5-5
export T2S_MAX_BYTES=53687091200   # 50 GB billed cap per query
export T2S_ROW_LIMIT=1000

streamlit run app.py        # UI
python eval_golden.py       # run before every prompt/model/schema change
```

## Security notes
- Read-only IAM; table allowlist and SELECT-only parse check; dry-run + maximum_bytes_billed.
- Query RESULTS are never sent back to the LLM, so data values cannot inject instructions.
- For per-user data access, run queries with the end user's credentials and use BigQuery row access policies.
