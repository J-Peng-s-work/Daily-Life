# Daily PQC Literature Digest

This folder contains a small daily literature pipeline for a PQC-focused PhD workflow.

It prioritizes IACR ePrint and supplements it with DBLP, Crossref, arXiv, and PubMed. Google Scholar is not included as an automated source because it does not provide a stable official public API for this use case; use Scholar Alerts as a separate manual backup.

## What It Sends

Each daily email contains at most 10 papers, ranked by PQC relevance and source priority. Every item includes:

- source, date, venue, authors, link
- labels: `密码学`, `protocol`, `implementation`, `theory`, `NTT`, `polynomial multiplication`, `PQC`
- Chinese summary
- English summary
- bilingual relevance note

## Setup

1. Copy the templates:

```powershell
Copy-Item .\config.example.json .\config.json
Copy-Item .\.env.example .\.env
```

2. Edit `config.json`:

- set `sources.crossref.mailto` to your email
- set `email.from` and `email.to`
- set `email.enabled` to `true` when ready to send email
- optionally set `llm.enabled` to `true` for stronger bilingual summaries. The current config uses DeepSeek's OpenAI-compatible `/chat/completions` API.

3. Edit `.env`:

- `SMTP_USERNAME`
- `SMTP_PASSWORD`, ideally an app password
- optional `DEEPSEEK_API_KEY` and `DEEPSEEK_MODEL`

## Test Once

```powershell
py .\pqc_digest.py --dry-run --include-seen
```

If `py` is not available, use the bundled Codex Python directly:

```powershell
& "C:\Users\126105287\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" .\pqc_digest.py --dry-run --include-seen
```

The report is written to `reports/YYYY-MM-DD.html` and `reports/YYYY-MM-DD.md`.

## Daily 08:00 Ireland Time on Windows

Run this from the `pqc_literature_digest` folder after `config.json` and `.env` are ready:

```powershell
$script = (Resolve-Path .\pqc_digest.py).Path
$workdir = (Get-Location).Path
# $python = "C:\Users\126105287\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$action = New-ScheduledTaskAction -Execute "py.exe" -Argument "`"$script`"" -WorkingDirectory $workdir
$trigger = New-ScheduledTaskTrigger -Daily -At 09:00
Register-ScheduledTask -TaskName "Daily PQC Literature Digest" -Action $action -Trigger $trigger -Description "Send bilingual daily PQC literature digest" -Force
```

Windows will use the machine's local timezone. On this machine, set it to Ireland time or adjust the trigger accordingly.

## Notes

- IACR ePrint is treated as the primary source.
- The script keeps a `seen_papers.json` file to avoid resending the same paper.
- If the LLM key is not configured, the digest still runs, but Chinese summaries become conservative metadata notes instead of real translations.
