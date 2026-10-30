# Conversation workflow

Conduct this conversation in the user's language. A bare skill invocation starts
the interview; it does not authorize fetching a default dataset.

## 1. Choose a source

If no source was supplied, the **first question** is:
"Do you want a ready/saved source, or an external platform?"

- **Ready:** run `--list-sources --json-output`, display every returned source as
  a numbered list, accept a number or platform name. Saved custom sources appear
  beside built-ins. Yahoo/yfinance are aliases.
- **External:** ask for the platform name or URL. Resolve a name to its official
  site and obtain the specific dataset/export/table URL. Never invent a URL or
  silently switch to a built-in source.

## 2. Collect 3–5 short data requirements

Skip information already supplied. Ask about topic/intended use, country/date
scope, required fields, desired row count and output format. Combine scope with
topic if useful. A necessary platform selector can replace a question. Routing
questions above are separate; do not ask five questions just to meet a quota.

Translate the goal into the provider's selector using discovery/candidate
metadata. Free text is not a ticker, indicator ID or Kaggle dataset slug.
`--goal` records context; **it does not apply content, date or country filters**.
Choose data with the requested coverage, or apply an explicit reviewed local
filter and verify the result before claiming fulfillment.

## 3. Check relevance and access

Explain what the source covers. If the request may not fit (e.g. aircraft details
from rental listings), explain the limitation and offer another source or an
inspection of a candidate sample. Consent to inspect never authorizes labelling
unrelated data as matching. Consider overlaps such as aircraft-company finances
in SEC filings. If relevant records/fields cannot be found, report it and suggest
another source.

For external sites inspect official APIs/exports, terms, licence, authentication,
robots.txt, quotas, rate limits and anti-bot restrictions. The analyzer checks
technical access only; it does not establish legal permission.

```text
data-fetcher --source custom --query URL --analyze-only --yes --non-interactive --json-output --outdir DIR
```

Read difficulty, robots policy, status and warnings. `mode: analysis` is not a
download. Blocked/hard targets need an authorized alternative or a reviewed
tailored API/browser extractor. Do not bypass controls or call a placeholder
successful extraction.

## 4. Show actual rows before final approval

Explain preview transfer cost first. Uncompressed CSV/TSV samples read a prefix
up to 256 KB; compressed/SDK previews may need a full transfer; custom sources
run a bounded 5-row sample through the isolated worker. Obtain consent to that
transfer when necessary.

```text
data-fetcher --source SOURCE --query SELECTOR --preview-only --yes --non-interactive --json-output --outdir DIR
```

Use optional `--columns a,b` for exact required fields or `--table INDEX` for a
simple HTML table.

**The preview JSON envelope contains no sample cell values and no column
names.** The human reads the sample from the private artifact
`<outdir>/<source>/<query>/_human_preview.md` (source URL, transfer scope,
column names, and the first five rows). Direct the human to open that file
themselves and confirm the sample matches their request. Do NOT read
`_human_preview.md` yourself, and never read `*_raw.csv`, `*_source_*`, or
`*_description.md` files to bypass the envelope redaction — the sample is the
human's alone to inspect. Relay the envelope's `source_url`, `transfer_scope`,
`rows_sampled`, `column_count` and `warnings`, then ask for final approval.
A path or "preview done" is insufficient: the human must actually review the
artifact. Five rows are a sample, not the completed dataset. Failed previews
must be resolved before final extraction.

For a bespoke custom extractor, write the reviewed script first (see
"Reviewed tailored extractors" below) and preview it with:

```text
data-fetcher --source custom --query URL --extractor-script PATH --preview-only --yes --non-interactive --json-output --outdir DIR
```

The preview returns a `preview_id`. Keep it: the approved full fetch must
present it.

## 5. Fetch the approved result

```text
data-fetcher --source SOURCE --query SELECTOR --yes --non-interactive --json-output --outdir DIR --output-format FORMAT
```

Every non-interactive **custom** full fetch (new, bespoke, or saved
`custom_*`) additionally requires the approved preview:

```text
data-fetcher --source custom --query URL --approve-preview PREVIEW_ID --yes --non-interactive --json-output --outdir DIR --output-format FORMAT --defer-source-choice
```

`--approve-preview` runs the extractor bytes staged with that exact preview —
never the original file, so later edits to the script file cannot swap what
runs. The URL, `--columns` and `--table` must match the previewed request or
the fetch fails with `APPROVAL_MISMATCH`. Built-in sources keep the plain
`--yes` behavior. Interactive terminal runs approve at the sample prompt
instead and do not need the flag.

Use `--rows N` only if requested, `--columns` only for exact field selection,
and the requested output format: csv/json/excel/parquet/sqlite. Inspect the
row count, warnings and conversion in the envelope (not the dataset files —
see the redaction rules above). Separate preview and full-fetch commands may
transfer data twice; disclose this. Row limits do not universally reduce
network traffic.

## 6. Ask whether to retain a new external platform

For an agent-managed custom fetch, add `--defer-source-choice` to the approved
full-fetch command. After successful extraction ask:
"Would you like to add this platform and extractor to our ready sources?"

- **Yes:** choose a platform name and run:
  `data-fetcher --keep-source PENDING_ID --save-source "Platform Name" --json-output`.
  Verify its `custom_...` key appears in `--list-sources`. No additional download.
- **No:** run `data-fetcher --discard-source PENDING_ID --json-output`. Only the
  pending script/descriptor are removed; downloaded data remains.
- Await the answer. Do not auto-register. A pending choice survives CLI exit.
  Without this flag, machine runs remove the temporary script automatically.
  Direct interactive CLI runs ask the retention question themselves.

Saved scripts are limited to their registered hostname. The generated template
supports public CSV/TSV/JSON/XLSX/Parquet and simple HTML tables. Ambiguous tables
need fields or a zero-based table index. Multiple JSON record arrays, pagination,
dynamic DOM, HTML rowspan/colspan and authenticated sites need reviewed tailored
code; do not claim arbitrary websites are automatically supported.

## Reviewed tailored extractors

Where the template cannot represent an authorized platform, the agent may write
and test a platform-specific script exposing:
`extract_frame(url=None, columns=None, table_index=None, row_limit=0) -> DataFrame`.
Reuse access checks, enforce transfer caps/timeouts, honor quotas and propagate
failure. Run it through the bounded-preview → human-approval flow above (the
script executes in an isolated worker process, never in the CLI process). Show
the human the `_human_preview.md` artifact before saving. Only after the
human's approval and retention answer, register with
`--approve-preview PREVIEW_ID --save-source "Platform Name"` (or resolve the
pending choice with `--keep-source`/`--discard-source`); registration always
stores the exact staged bytes that were previewed. Never register arbitrary
downloaded code without reviewing it. The generated-code mechanism is reviewed
local execution with fail-fast checks and runtime defense-in-depth — it is not
a sandbox (see `SECURITY.md`).

## Direct terminal interview and storage

`data-fetcher` with no arguments asks ready/external first, lists numbered sources,
collects up to five requirements, previews actual rows, asks final approval even
with a preset row limit, converts the requested format and asks about retention.
For a platform name alone the terminal asks for its public dataset/page URL;
the conversational agent can perform discovery on the user's behalf.

Saved descriptors: `~/.config/data-fetcher-pipeline/sources.json` (versioned).
Managed reusable scripts: `source_scripts/`; unresolved choices: `pending_sources/`.
Registration uses atomic writes and rejects existing names. Checksums detect
script changes; corrupt descriptors fail explicitly rather than hiding sources.
Concurrent registrations return `REGISTRY_BUSY` rather than overwriting each
other. After a crashed writer, remove its stale `sources.lock` only once no
registration is running.
