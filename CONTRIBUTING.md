# Contributing

Thanks for your interest in improving this project. Issues and pull requests are
welcome — for the **code**: the pipeline, the web app, classification logic,
docs, and tests.

## Ground rules on data (read this first)

This repo ships a small **synthetic** sample database (`web/data/sample.db`) so
the app runs out of the box. **The real alumni dataset is not in this repo and
must never be committed.** When you contribute:

- Do **not** add real personal data, real scraped profiles, or a real
  `titans.db` to the repo. The `.gitignore` blocks the obvious paths; don't work
  around it.
- Do **not** paste real names, emails, or other PII into issues, PRs, commit
  messages, or test fixtures. Use the synthetic sample data.
- If you're an alum and want a correction or removal, see
  [SECURITY.md](SECURITY.md) — don't open a public issue with personal details.

## Project layout

| Path | What it is |
|---|---|
| `pipeline/` | Python: collection, enrichment, classification, roll-ups |
| `web/` | Next.js app: directory, insights, chat |
| `web/data/sample.db` | The synthetic DB the app reads in development |
| `docs/` | Design notes and plans |
| `RUNBOOK.md` | Operator's guide to running the pipeline |

See [README.md](README.md) for the architecture overview.

## Getting set up

**Web app** (works with the bundled sample DB, no keys needed):

```bash
cd web
npm install
npm run dev        # http://localhost:3210
```

**Pipeline** (requires your own API keys — see [`.env.example`](.env.example)):

```bash
cd pipeline
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

You bring your own keys (Anthropic, PDL, Perplexity, …). The project does not
provide data or credentials.

## Making a change

1. **Branch** off `main`.
2. **Tests** — add or update them. Run the suites:
   - Pipeline: `cd pipeline && pytest`
   - Web: `cd web && npm test`
3. **Lint / types** (web): `npm run lint`.
4. **Conventional commits** for messages: `feat:`, `fix:`, `refactor:`,
   `docs:`, `test:`, `chore:`, `perf:`, `ci:`.
5. **Open a PR** describing the change and how you verified it. Fill in the PR
   template.

## Style

- **Python:** PEP 8, type hints, small focused functions, docstrings.
- **TypeScript:** explicit types on public APIs, prefer immutability, no
  `console.log` in committed code.
- Many small files over a few large ones. Keep modules focused.

## Regenerating the sample DB

If a schema change makes the sample DB stale, regenerate it (never commit the
real DB):

```bash
python pipeline/make_sample_db.py
```

That's it. Thanks for contributing.
