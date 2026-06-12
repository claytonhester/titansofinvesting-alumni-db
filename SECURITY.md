# Security & Privacy Policy

## Reporting a security vulnerability

Please **do not** open a public issue for security vulnerabilities.

Instead, email **claytonhester10@gmail.com** with:

- a description of the issue,
- steps to reproduce (or a proof of concept), and
- the potential impact.

You'll get an acknowledgement, and we'll work on a fix before any public
disclosure.

## Data, privacy, and removal requests

This project assembles a directory from a **public** class roster and enriches
it with **publicly available, source-attributed** career data. This repository
ships only a **synthetic sample dataset** — the real dataset is not published
here.

If you are an alum (or your representative) and want your information
**corrected or removed** from any operated instance of this project:

- Email **claytonhester10@gmail.com** with the name and the correction/removal
  request.
- **Do not** post personal details in a public GitHub issue.

Operators of this software are responsible for honoring removal requests for the
data they collect and host.

## Handling secrets

- API keys and secrets live in environment variables (`.env`, which is
  gitignored) — never in source.
- Never commit a real `.env`, real credentials, or the real `titans.db`.
- If you believe a secret was committed, rotate it immediately and email the
  address above.
