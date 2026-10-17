# Security policy

## What this project handles

The radar calls public scholarly, archive, regulatory and company-data APIs on your behalf and,
optionally, an LLM API, an SMTP server and a Slack webhook. Every credential is optional and belongs
to the person running the copy. The tool never logs in to a publisher website and never asks for a
publisher username or password (see `docs/LEGAL.md`).

## Where credentials live, and where they never go

* Locally: a git-ignored `.env` next to the code. The template `.env.example` ships with every value
  empty or set to an `example.com` placeholder.
* On GitHub Actions: repository **secrets** (*Settings -> Secrets and variables -> Actions*).
  Non-secret settings (the model name, whether a fork commits digests) are repository **variables**.
* Nowhere else. Not in `config/*.yaml`, not in a digest, not in an issue or pull request, not in the
  SQLite database or the HTTP cache (both hold only harvested public content), not in a workflow
  file. A pull request that adds a credential to YAML or Markdown will be closed.

## What the repository does to protect them

* `ci.yml` references no secrets, so pull requests from forks run with a read-only token and nothing
  to leak; `weekly.yml` has no `pull_request` trigger.
* `weekly.yml` splits into a job that holds the API keys but can only read the repository
  (`contents: read`, `persist-credentials: false`) and a job that can write (release assets, the
  digest commit) but holds no API key. `permissions: {}` is the workflow default; each job asks for
  what it needs.
* Every third-party action is pinned to a full commit SHA, with the release tag in a comment.
* `workflow_dispatch` inputs reach the shell only through `env:` variables and are validated
  (`days` must be an integer; the time-series vendor token is restricted to a safe character set).
* CI fails if a database, WAL/SHM file, compressed snapshot, cache directory or `.env*` file (other
  than `.env.example`) is ever tracked.
* YAML configuration is loaded with `yaml.safe_load`.

## Known limitations

* Keys that travel in a URL query string (the NCBI `api_key`, the openFDA `api_key`) can appear in
  the HTTP client's retry and failure log lines (`GET <url> -> 5xx; retrying`). Keep cron/launchd
  logs private, redact URLs before pasting a log into an issue, and rotate the key if a log was
  shared. GitHub Actions masks the exact secret string in its logs, but not an encoded form of it.
* SMTP: check that `litradar/digest/deliver.py` passes `ssl.create_default_context()` to
  `starttls()` in your checkout. If it does not, Python's default STARTTLS context does not verify
  the server certificate, and an on-path attacker could capture the SMTP password; use a disposable
  app-specific password and a trusted network until it does.
* Digest Markdown is rendered from paper titles, abstracts and LLM output. If you forward the HTML
  e-mail to others, treat it as untrusted content from the web, which is what it is.
* The database snapshot (release asset) and the committed digests are as public as the repository.
  They contain no credentials, but they do reveal which companies, instruments and people you watch.

## What counts as a vulnerability here

Please report:

* any path by which a user-supplied credential is exposed: log lines, exception text, the HTTP
  cache, the database, a digest, an e-mail, a Slack post, a workflow log or an artifact;
* any way for repository content or fetched data to execute code or shell: expression injection in
  a workflow, unsafe YAML loading, HTML or script injection in the rendered digest or e-mail;
* any way for a pull request from a fork to reach secrets or write to the repository;
* a tampered dependency or a pinned action that no longer matches its release;
* requests to hosts outside the documented sources that can be induced by data (SSRF through
  configuration URLs or fetched records).

Not vulnerabilities (open a normal issue instead): HTTP 429s or a source blocking the User-Agent,
licence and terms-of-service questions (see `docs/LEGAL.md`), and findings that require the
attacker to already hold write access to the repository.

## Reporting

Use GitHub's private vulnerability reporting on this repository (*Security -> Advisories -> Report
a vulnerability*). Do not file a public issue for credential leaks or injection bugs. Expect an
acknowledgement within 7 days. Include the commit SHA, the workflow run or log excerpt (redacted),
and reproduction steps.

## Key rotation

Rotate on a schedule you are comfortable with, and immediately if a key was ever pasted anywhere it
should not have been. Each provider's console lets you create the replacement first and then revoke
the old key, so the weekly run never breaks:

| Credential | Where to rotate | Blast radius if leaked |
|---|---|---|
| `ANTHROPIC_API_KEY` | provider console -> API keys (disable, then delete); set a monthly spend limit on the billing page and create keys with an expiry | paid API usage on your account |
| `NCBI_API_KEY` | NCBI account settings -> API Key Management (one key per account) | rate-limit abuse attributed to you |
| `OPENALEX_API_KEY` | OpenAlex account settings -> API key | your free daily budget consumed |
| `SEMANTIC_SCHOLAR_API_KEY`, `OPENFDA_API_KEY` | request a new key from the provider and ask them to retire the old one | rate-limit abuse attributed to you |
| `SMTP_PASSWORD` | your mail provider's app-password page; delete the old app password | mail sent as you |
| `SLACK_WEBHOOK_URL` | the Slack app's Incoming Webhooks page: delete and recreate | anyone can post to the channel |

After rotating, update the GitHub Actions secrets and every `.env` on every machine.

## If a key was pushed

1. **Revoke or rotate first.** A pushed secret is public the moment it is pushed; rewriting history
   does not un-leak it, and forks keep the old commits.
2. Check the provider's usage or billing page for unexpected activity.
3. Then scrub: on a fresh mirror clone, `git filter-repo --sensitive-data-removal --replace-text
   <file with the literal value>` (or `--invert-paths --path .env` if a whole file was committed),
   force-push, and ask GitHub Support to purge cached views.
4. Re-run the weekly workflow by hand with the new secrets and confirm it is green.
