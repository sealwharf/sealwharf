# Project instructions

## Rules

- Write all repository content in English, including source code, identifiers,
  filenames, comments, documentation, test data, and runtime messages. The chat
  messages of the bot are translated in `src/sealwharf/locales/`.
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

- `src/sealwharf/documents/`: DOCX and ODT conversion, signature positioning, appearance,
  signing, validation and signature verification.
- `src/sealwharf/application/`: `signers.py` opens the certificates;
  `worker.py` is the request flow (roles, owner approval, sequential
  signatures, commands, audit). `history.py` answers `bot log` with what
  each person may read from the record of signatures.
- `src/sealwharf/adapters/`: the WhatsApp gateways (`gateway.py` is what the bot
  needs from any of them, `gateways.py` lists the adapters, `whapi.py` and
  `waha.py` are one adapter each), inbox queue, audit record, worker lock and
  the local AI model (`local_ai.py`, `model_runtime.py`). The model service
  (`ollama`, with the `model` download step in `compose.yaml`) is part of the
  bot: it starts by default and the bot does not start without it.
- `src/sealwharf/session.py`, `check.py`, `token_tools.py`, `healthcheck.py`,
  `purge.py`, `logs.py`, `__main__.py`: container entry points (`run`,
  `check`, `token-driver`, `token-check`, `token-retry`, `healthcheck`,
  `purge`, `log`).
- `src/sealwharf/signer_config.py`: the single YAML configuration, secrets included;
  `limits.py` holds the limits each installation chooses under `limits`.
- `src/sealwharf/locales/*.toml`: one catalog per language (`en` is the
  reference, plus `pt-br` and `es`) with every chat message, the wake replies
  and the phrases people may write. `src/sealwharf/messages.py` loads and
  checks them; no user-facing text lives in the Python code.
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

- When the signer identity is ambiguous, the document must not be signed: the
  full name, or a registered alias, must be written in the document, and not
  only inside a sentence about the person. Changes to name matching must
  preserve this and cover the ambiguous case with a test.
- A place to sign is an empty area the code found and checked. The local model
  only picks one of those places by letter, never gives coordinates, and is not
  followed when the document says otherwise (another block names the person,
  or the label's role belongs to someone else). Two people given one place get
  neither. When no place is clear, the bot signs only where the requester
  agrees (the end of the last page), and every signed document goes back with
  a request to review it.
- A certificate is used without the owner's approval only for its owner or a
  trusted contact. An approval covers the exact document that was sent, comes
  from the owner's number, is single-use and expires. People answer with a
  plain 1 or 0, so a person never has two questions open at once: further
  questions wait in line (`ask_questions` in `worker.py`). One question may
  cover several documents only when it is the same question, and each
  document is still checked against its own hash before it is signed.
- A waiting question is never dropped in silence: it is asked, expires after
  being asked, or its requester is told why it was closed.
- A gateway adapter reports the number WhatsApp gives for a sender and drops the
  message when the gateway cannot tell it. Only the inbox decides who may use
  the bot. A new adapter needs a test for the unknown sender.
- Secrets live only in `config/` (`sealwharf.yaml`, and `tunnel-token`, which the
  tunnel container reads) and never appear in logs, error
  messages, backups or `repr` output. Operational logs carry no phone numbers,
  names, file names or document content.
- A new chat message needs a text in every language file in `locales/`; a
  catalog that is incomplete stops the program at start. A phrase people write
  (wake phrase, answer word, verify caption) goes in the catalogs too, never
  in the code, and an answer word may not mean different answers in two
  languages. The exception is the commands (`bot status`, `bot log`,
  `bot help`), written the same way by everyone and kept in
  `adapters/inbox.py`; only their answers are translated.
- A finished document leaves only its entry in the record of signatures
  (file name and fingerprints, never content): its copy and the details in the
  queue are erased when it is done, and its queue entry within an hour.
  New stored state needs an erasing path, and `purge` must remove it.
- Through the chat a person reads only the log entries they asked for,
  approved, or that used a certificate they own (`application/history.py`).
  The record of signatures is erased after `log_days`.
- A token PIN is tried once. After a refusal the guard file in the data folder
  blocks every further login until `token-retry` is run; never loop on a PIN.
