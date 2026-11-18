# Project instructions

## Rules

- Write all repository content in English, including source code, identifiers,
  filenames, comments, documentation, test data, and runtime messages. The chat
  messages of the bot also exist in Portuguese, in `messages.py`.
- Communicate with the project owner in Portuguese unless requested otherwise.
- Never commit certificates, credentials, private documents, or local configuration.
- Use Angular-style commit messages and review staged changes before each commit.
- Commit only in the owner's name: no co-author or tool attribution lines.
- Push completed commits to the configured remote without waiting for a separate request.
- Run tests and static checks relevant to the changes. Avoid rerunning the full
  suite or unrelated analysis unless the change or an unresolved failure warrants it.

## Project

Sealwharf signs PDF and DOCX documents received through WhatsApp using P12/PFX
certificates or PKCS#11 tokens. It runs only as a Docker container. The Python
package is `sealwharf`.
`README.md` is the user guide: keep it step by step, in plain words, with every
command a reader needs.

## Layout

- `src/sealwharf/documents/`: DOCX conversion, signature positioning, appearance,
  signing, validation and signature verification.
- `src/sealwharf/application/`: `signers.py` opens the certificates;
  `worker.py` is the request flow (roles, owner approval, sequential
  signatures, commands, audit).
- `src/sealwharf/adapters/`: Whapi, webhook settings, inbox queue, audit
  record, worker lock and the local AI model.
- `src/sealwharf/session.py`, `check.py`, `token_tools.py`, `healthcheck.py`,
  `__main__.py`: container entry points (`run`, `check`, `token-driver`,
  `token-check`, `token-retry`, `healthcheck`).
- `src/sealwharf/signer_config.py`: the single YAML configuration, secrets included.
- `src/sealwharf/messages.py`: every chat message, in `en` and `pt-br`.
- `Dockerfile`, `compose.yaml`, `docker/fonts.conf`, `config/sealwharf.example.yaml`.
- `config/`, `data/`: private local state, ignored by Git except the example
  and requirement files. `config/sealwharf.yaml` holds every password and
  token: never print it, quote it or copy it into commits or output.

## Commands

The worker-lock tests need POSIX and are skipped on Windows; the image build is
the check for container changes.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
.\.venv\Scripts\python.exe -m pytest -q tests\test_flow.py
.\.venv\Scripts\python.exe -m ruff check src tests
docker build -t sealwharf:local .
```

## Safety invariants

- When a signature location or signer identity is ambiguous, the document must
  not be signed. Changes to positioning or name matching must preserve this and
  cover the ambiguous case with a test.
- A certificate is used without the owner's approval only for its owner or a
  trusted contact. An approval covers the exact document that was sent, comes
  from the owner's number, is single-use and expires. The owner answers with a
  plain 1 or 0, so an owner never has two requests open at once.
- Secrets live only in `config/sealwharf.yaml` and never appear in logs, error
  messages, backups or `repr` output. Operational logs carry no phone numbers,
  names, file names or document content.
- A new chat message needs a text in every language in `messages.py`.
- A token PIN is tried once. After a refusal the guard file in the data folder
  blocks every further login until `token-retry` is run; never loop on a PIN.
