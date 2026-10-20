"""Text-to-SQL core: generate -> validate -> dry-run -> execute, with guardrails."""
import json
import os
import time
from functools import lru_cache

import sqlglot
import yaml
from anthropic import Anthropic
from google.cloud import bigquery
from sqlglot import exp

MODEL = os.getenv("T2S_MODEL", "claude-sonnet-5-5")
MAX_BYTES = int(os.getenv("T2S_MAX_BYTES", 50 * 1024**3))   # 50 GB billed cap
ROW_LIMIT = int(os.getenv("T2S_ROW_LIMIT", 1000))
MAX_RETRIES = 2
LOG_PATH = os.getenv("T2S_LOG", "queries.log.jsonl")

with open(os.path.join(os.path.dirname(__file__), "semantic_layer.yaml")) as f:
    CFG = yaml.safe_load(f)

ALLOWED = {t["name"].lower(): t for t in CFG["tables"]}

FORBIDDEN = tuple(
    getattr(exp, n)
    for n in ("Insert", "Update", "Delete", "Drop", "Create", "Alter", "Merge", "Command", "Copy")
    if hasattr(exp, n)
)


# ---------------------------------------------------------------- prompt
def build_system_prompt() -> str:
    tables = []
    for t in CFG["tables"]:
        cols = "\n".join(f"    - {c['name']} ({c['type']}): {c['description']}" for c in t["columns"])
        part = f"\n  PARTITION COLUMN: {t['partition_column']} (must be filtered)" if t.get("partition_column") else ""
        tables.append(f"- `{t['name']}`: {t['description']}{part}\n{cols}")
    metrics = "\n".join(f"- {m['name']}: {m['description']} = {m['sql']}" for m in CFG["metrics"])
    rules = "\n".join(f"- {r}" for r in CFG["rules"])
    examples = "\n\n".join(f"Q: {e['question']}\nSQL:\n{e['sql']}" for e in CFG["examples"])
    return f"""You translate marketing questions into BigQuery Standard SQL.

TABLES (use ONLY these, fully qualified, in backticks):
{tables}

CERTIFIED METRICS (use these formulas exactly):
{metrics}

RULES:
{rules}
- Produce exactly ONE read-only SELECT statement. No DDL/DML, no scripting.
- Always filter the partition column of any partitioned table.
- Select only needed columns. Do not use SELECT *.
- If the question is ambiguous in a way that changes the numbers, or cannot be answered from these
  tables, set needs_clarification=true and ask ONE short question. Do not guess.
- List every assumption you made (date range, metric definition, filters) in `assumptions`.

EXAMPLES:
{examples}
"""


SYSTEM_PROMPT = build_system_prompt()

SUBMIT_TOOL = {
    "name": "submit_sql",
    "description": "Return the SQL answer or a clarifying question.",
    "input_schema": {
        "type": "object",
        "properties": {
            "needs_clarification": {"type": "boolean"},
            "clarifying_question": {"type": "string"},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "sql": {"type": "string"},
        },
        "required": ["needs_clarification", "assumptions"],
    },
}


# ---------------------------------------------------------------- generate
@lru_cache(maxsize=1)
def _llm() -> Anthropic:
    return Anthropic()  # reads ANTHROPIC_API_KEY


def generate(question: str, feedback: dict | None = None) -> dict:
    user = question
    if feedback:
        user += (
            f"\n\nYour previous SQL was rejected.\nSQL:\n{feedback['sql']}\n"
            f"Error: {feedback['error']}\nReturn corrected SQL."
        )
    resp = _llm().messages.create(
        model=MODEL,
        max_tokens=1500,
        temperature=0,
        system=SYSTEM_PROMPT,
        tools=[SUBMIT_TOOL],
        tool_choice={"type": "tool", "name": "submit_sql"},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("Model returned no structured output")


# ---------------------------------------------------------------- validate
def validate(sql: str) -> str:
    """Parse with a real SQL parser. Returns safe SQL (with LIMIT) or raises ValueError."""
    if not sql or not sql.strip():
        raise ValueError("Empty SQL")
    try:
        statements = sqlglot.parse(sql, read="bigquery")
    except sqlglot.errors.ParseError as e:
        raise ValueError(f"SQL parse error: {e}")
    if len(statements) != 1 or statements[0] is None:
        raise ValueError("Exactly one statement is allowed")
    tree = statements[0]

    if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery)):
        raise ValueError("Only SELECT statements are allowed")
    if any(tree.find_all(*FORBIDDEN)):
        raise ValueError("Write/DDL operations are not allowed")

    # Table allowlist (CTE names are exempt)
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    referenced = set()
    for t in tree.find_all(exp.Table):
        if not t.catalog and not t.db and t.name.lower() in cte_names:
            continue
        fqn = ".".join(p for p in (t.catalog, t.db, t.name) if p).lower()
        if fqn not in ALLOWED:
            raise ValueError(f"Table not allowed: {fqn or t.name}")
        referenced.add(fqn)
    if not referenced:
        raise ValueError("Query references no allowed tables")

    # Partition filter required
    where_cols = {c.name.lower() for w in tree.find_all(exp.Where) for c in w.find_all(exp.Column)}
    for fqn in referenced:
        pcol = ALLOWED[fqn].get("partition_column")
        if pcol and pcol.lower() not in where_cols:
            raise ValueError(f"Missing partition filter on {pcol} for {fqn}")

    # Row limit
    if isinstance(tree, exp.Select) and not tree.args.get("limit"):
        tree = tree.limit(ROW_LIMIT)

    return tree.sql(dialect="bigquery")


# ---------------------------------------------------------------- execute
@lru_cache(maxsize=1)
def _bq() -> bigquery.Client:
    return bigquery.Client(project=CFG["project"])


def dry_run(sql: str) -> int:
    job = _bq().query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
    return job.total_bytes_processed


def run_query(sql: str):
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES, labels={"app": "text2sql"})
    job = _bq().query(sql, job_config=cfg)
    df = job.result(timeout=60).to_dataframe()
    return df, job.total_bytes_billed


def _log(record: dict) -> None:
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# ---------------------------------------------------------------- orchestrate
def answer(question: str) -> dict:
    feedback, t0 = None, time.time()
    for attempt in range(MAX_RETRIES + 1):
        out = generate(question, feedback)
        assumptions = out.get("assumptions", [])

        if out.get("needs_clarification"):
            _log({"q": question, "status": "clarify"})
            return {"status": "clarify", "question": out.get("clarifying_question", ""), "assumptions": assumptions}

        raw_sql = out.get("sql", "")
        try:
            safe_sql = validate(raw_sql)
            est = dry_run(safe_sql)
            if est > MAX_BYTES:
                raise ValueError(f"Query would scan {est/1e9:.1f} GB, over the {MAX_BYTES/1e9:.0f} GB cap. Narrow the date range or columns.")
        except Exception as e:  # validation, dry-run, or cost failure -> feed back to model
            feedback = {"sql": raw_sql, "error": str(e)}
            continue

        df, billed = run_query(safe_sql)
        _log({"q": question, "sql": safe_sql, "rows": len(df), "bytes_billed": billed,
              "attempts": attempt + 1, "secs": round(time.time() - t0, 2), "status": "ok"})
        return {"status": "ok", "sql": safe_sql, "data": df, "assumptions": assumptions, "bytes_billed": billed}

    _log({"q": question, "status": "failed", "error": feedback["error"]})
    return {"status": "failed", "error": feedback["error"]}
