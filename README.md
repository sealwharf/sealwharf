<p align="center">
  <img src="src/sealwharf/assets/default-logo.png" alt="Sealwharf logo" width="96">
</p>

<h1 align="center">Sealwharf</h1>

<p align="center">Sign documents by sending them to a WhatsApp number.</p>

Send a PDF or Word document and get it back digitally signed. Sealwharf runs on your own computer, in Docker, so your certificates and documents never leave it.

- It finds the right place to sign by looking for the signer's name next to a signature line.
- It can sign with certificate files (`.p12`, `.pfx`) and with USB tokens.
- When someone asks to sign with another person's certificate, that person approves first, with a single reply.

## What you need

- A computer with [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker with Compose on Linux).
- A channel on [Whapi.Cloud](https://whapi.cloud), connected to the WhatsApp number that will be the bot.
- A domain managed by [Cloudflare](https://dash.cloudflare.com), to give the bot a fixed address.
- Your certificate: a `.p12` or `.pfx` file with its password, or a USB token with its PIN.

## Set it up in five steps

### 1. Download the project

Download this repository (the green **Code** button, then **Download ZIP**, and unzip it), or clone it with Git.

Open a terminal inside the project folder. Every command below is run from there.

### 2. Create the address of the bot

In the Cloudflare dashboard, open **Networking → Tunnels** and create a tunnel. Give it any name.

Cloudflare shows a command to install a program. **Do not install anything.** Just copy the long text at the end of that command, starting with `eyJ`: that is the *tunnel token*. Leave the page open; you will come back to it in step 5.

### 3. Get the WhatsApp token

In the Whapi dashboard, open your channel and copy its **API token**. The channel must be connected (QR code scanned).

If you run a Whapi-compatible service of your own instead of Whapi.Cloud, use its token here and add its address to the configuration in step 4, in a line such as `whapi_url: http://host.docker.internal:8787`.

### 4. Fill in the configuration

Create a folder named `certificates` inside `config/` and copy your certificate file into it. Then make a copy of `config/sealwharf.example.yaml` named `config/sealwharf.yaml` and fill it in. This is the smallest working file:

```yaml
public_url: https://bot.example.com        # the address you will create in step 5
whapi_token: "the token from step 3"
tunnel_token: "the token from step 2"

contacts:
  john:
    name: John Doe
    number: "5511999999999"                # country code, area code and number; digits only, in quotes

certificates:
  - id: john
    name: John Doe                         # the full name, as in the certificate; capitals and accents do not matter
    file: certificates/john.p12
    password: "the certificate password"   # always in quotes
    owner: john
```

This file holds passwords, so keep it to yourself. Git ignores the whole `config/` folder.

### 5. Check, start and publish

Check the file. This opens every certificate, tests the WhatsApp token, and tells you exactly what to fix:

```bash
docker compose run --rm sealwharf check
```

The first run takes a few minutes, while Docker builds the program. When the check ends with `Ready to start.`, start the bot:

```bash
docker compose up -d
```

Go back to the Cloudflare page: the tunnel now appears as connected. Continue, choose **Add route → Published application**, and fill in:

- **Subdomain and domain:** the address you wrote in `public_url`, for example `bot` + `example.com`.
- **Service URL:** `http://localhost:8000`

A minute later the bot is ready. To confirm, send it the words `wake up bot` from your WhatsApp (or `acorda bot`, or `despierta bot`): it answers with a short, playful line, such as "I'm awake, just resting my eyes."

## Using it

Send a PDF or Word document to the bot. It answers that it received the file and, a few seconds later, sends back the signed PDF.

For the bot to find where to sign, **the document needs the signer's full name next to an empty signature line**, like this:

```
        ______________________________
                  John Doe
```

Only the people who sign need that. A name that appears only inside a sentence, such as the person a declaration is about, is not taken as a signer.

Capital letters and accents do not matter. `José Gonçalves`, `Jose Goncalves` and `JOSE GONCALVES` are the same name for the bot, so a document typed without accents is signed just the same.

| You send | The bot does |
| --- | --- |
| A PDF or Word document | Signs it and sends it back. |
| `1`, `2`, `0`… or a word such as `yes`, `no`, `all` | Takes it as your answer to the question it just asked. |
| A PDF with the caption `verify` (or `verificar`) | Checks the signatures of that PDF, without signing. |
| `wake up bot` | Answers with a short, playful line, to show it is working. |
| `bot status` | Lists the installed certificates with their expiry dates, and how many documents wait for an answer. |

Anything else you write is ignored, so the number can still be used for ordinary conversations.

### The two commands

Both commands work only for the numbers in `contacts`, and only when the message is exactly the command. Capital letters do not matter, and the words may be written together (`wakeupbot`, `botstatus`). A message that merely resembles a command, such as `wake up bot!` or `status`, gets no answer.

**`wake up bot`** is the quick test that the bot is alive. It answers with a line picked at random, in whatever mood it happens to be: sleepy, eager, grumpy, dramatic or sarcastic. For example:

> Zzz... sign here... zzz... oh! Hi. I'm awake.

> You woke me up for this? There had better be a document coming.

> Pen? Got it. Certificate? Got it. Will to live? Loading...

Each language has its own phrase (`wake up bot`, `acorda bot`, `despierta bot`). All three work for everyone, and the answer comes in the language of whoever asked.

**`bot status`** shows how the bot is doing. It is written the same way in every language, and the answer comes in the language of whoever asked:

```text
Up and running. Certificates installed: 2.
1. John Doe (file): valid until 2027-08-14, 268 day(s) left
2. Jane Doe (USB token): expires on 2026-12-10, only 21 day(s) left. Time to renew it!
Documents waiting for an answer: 0.
```

- Each certificate shows the name of its holder, whether it is a file or a USB token, the date it expires and the days left.
- A certificate with 30 days or less is marked for renewal, and one past its date is marked `EXPIRED`.
- The last line counts the documents that still wait for somebody's answer.

### Languages

The bot speaks English (`en`), Brazilian Portuguese (`pt-br`) and Spanish (`es`). Each person reads the language set for them in `contacts`, and everyone may write in any of the three:

| | English | Português | Español |
| --- | --- | --- | --- |
| Check that the bot is working | `wake up bot` | `acorda bot` | `despierta bot` |
| Answer yes | `yes`, `approve`, `ok` | `sim`, `aprovar` | `sí`, `aprobar`, `vale` |
| Answer no | `no`, `reject`, `cancel` | `não`, `recusar`, `cancelar` | `no`, `rechazar`, `cancelar` |
| Answer all | `all` | `todos` | `todos` |
| Caption to check signatures | `verify` | `verificar` | `comprobar` |

### Who signs and who approves

**When more than one person can sign.** If the document has the names of several signers you may use, the bot asks who should sign and you reply with a number: one of them, all of them, or cancel.

**When the certificate belongs to someone else.** The bot tells you that the owner must approve and asks if it should send them the document. If you agree, the owner receives the PDF with a message like this:

> Hello! Jane Doe (+55 21 88888-8888) asks you to sign the following document with your digital certificate. Reply 1 to approve. Reply 0, or simply ignore this message, to refuse.

When the owner replies `1`, the document is signed and sent back to you.

**When a signature cannot be made.** If the certificate of one of the signers is expired, revoked or cannot be validated, or its USB token is not connected, the bot does not sign and tells you whose signature had the problem:

> Sorry, I could not sign this document: the certificate of John Doe is expired, revoked or could not be validated. Nothing was signed.

If the others had already signed, you receive the document with their signatures and a note saying which one is missing and why.

### Sending many documents at once

Select all the files and send them together. The bot waits about five seconds after your last file, so that everything you sent together is handled as one batch:

- It says once that it received them, and at the end sends one summary: which files were signed and which were not, and why.
- If it needs to ask you something, it asks once for all the documents that have the same question.
- If an owner must approve, the owner gets one message with the list and all the files, and answers once: `1` approves all of them, `0` refuses all of them.
- The same file sent twice is handled once.

### The waiting line

Each person has only one open question at a time, so a plain `1` or `0` never answers the wrong document. Every other question waits in line for its turn.

- **A new document arrives while the bot waits for your answer.** The first question stays open and your answer goes to it. The question about the new document is asked right after.
- **The owner is still answering someone else.** Your request waits in line, and the bot tells you how many requests are ahead of yours:

  > Your document is in line for the approval of John Doe, with 2 request(s) ahead of it. I will ask as soon as it is its turn; you do not need to send it again.

- **The time to answer** (5 minutes) starts only when the question is actually asked, never while it waits in line.
- **No question is dropped in silence.** If nobody answers in time, nothing is signed and whoever sent the document is told so.
- **The line has a limit.** Up to 20 documents may wait for the same owner. Past that, the bot says the owner has too many requests waiting and asks you to send the document again later.

To see how many documents are waiting right now, send `bot status`.

## Adding people and certificates

Everything lives in `config/sealwharf.yaml`. Register each person once under `contacts`, then use their ids in the certificates:

```yaml
language: pt-br                    # language for contacts that set no lang: en, pt-br or es

contacts:
  john:
    name: John Doe
    number: "5511999999999"
    lang: en                       # this person reads English
  jane:
    name: Jane Doe
    number: "5521888888888"
  mary:
    name: Mary Major
    number: "5531777777777"

certificates:
  - id: john
    name: John Doe
    file: certificates/john.p12
    password: "certificate-password"
    owner: john                    # signs directly, and approves the requests of preparers
    preparers: [jane]              # may ask; the owner approves each document (one name may be written alone: jane)
    trusted: [mary]                # may ask; signed without asking the owner
```

| Role | What it means |
| --- | --- |
| `owner` | The person the certificate belongs to. Their documents are signed at once, and they approve other people's requests. |
| `preparers` | People who prepare documents for the owner. Each request waits for the owner's approval. |
| `trusted` | People the owner trusts completely. Their requests are signed without asking. |

A number that is not in `contacts` is ignored by the bot.

After any change, check and restart:

```bash
docker compose run --rm sealwharf check
```

```bash
docker compose restart sealwharf
```

`config/sealwharf.example.yaml` lists every setting, including the optional ones.

## Signing with a USB token

A certificate on a USB token (such as an ICP-Brasil A3) works too. The setup has more steps because the token must be made visible to Docker, but two commands do the hard part for you.

### 1. Let Docker see the token (Windows only)

On Linux, skip this step. On Windows, Docker runs in a small virtual machine, so the token has to be handed to it with a free tool called usbipd-win.

Open PowerShell **as administrator** and install it:

```bash
winget install --exact dorssel.usbipd-win
```

Close PowerShell, open it again as administrator, plug in the token and list the USB devices:

```bash
usbipd list
```

Find the line of your token (it usually says *Smartcard Reader*) and note its `BUSID`, such as `2-1`. Share it once:

```bash
usbipd bind --busid 2-1
```

Then hand it to Docker. **Repeat this command every time you restart the computer or replug the token:**

```bash
usbipd attach --wsl --busid 2-1
```

If it answers `WSL kernel is not USBIP capable`, run this once and then repeat the `attach` command:

```bash
docker run --rm --privileged --pid=host debian:bookworm-slim nsenter -t 1 -m -u -n -i modprobe vhci-hcd
```

### 2. Start the card reader

```bash
docker compose --profile token up -d reader
```

### 3. Ask what the token holds

This command does not use the PIN. It tells you whether the token can be read and prints the lines to copy into the configuration:

```bash
docker compose run --rm sealwharf token-check
```

If it says that no installed library can read the token, your token needs the driver of its vendor:

1. Download the vendor's **Linux 64-bit** driver, in the version for **Ubuntu 22.04** (a `.deb` file, sometimes inside a `.zip`). For G+D StarSign tokens, common in Brazil, it is SafeSign, at [safesign.gdamericadosul.com.br/download](https://safesign.gdamericadosul.com.br/download).
2. Put the downloaded file, as it is, in the `config/pkcs11/` folder.
3. Install it and check again:

   ```bash
   docker compose run --rm sealwharf token-driver
   ```

   ```bash
   docker compose run --rm sealwharf token-check
   ```

### 4. Add the token to the configuration

Use the `module` line and the holder's name that `token-check` printed, and the token's PIN as the password:

```yaml
  - id: jane
    name: Jane Doe
    token:
      module: /data/pkcs11/libaetpkss.so.3
    password: "123456"                 # the PIN of the token, in quotes
    owner: jane
```

### 5. Check and start

```bash
docker compose run --rm sealwharf check
```

```bash
docker compose --profile token up -d
```

**About the PIN.** Tokens lock themselves after a few wrong PINs. To protect yours, the bot tries the PIN once; if the token refuses it, the bot stops and does not try again, even after a restart. Make sure it is the user PIN (not the PUK or administrator PIN), correct it in the configuration, and release one new attempt with:

```bash
docker compose run --rm sealwharf token-retry
```

## When something goes wrong

Start with the check. It names the problem in plain words:

```bash
docker compose run --rm sealwharf check
```

| What you see | What to do |
| --- | --- |
| The bot does not answer `wake up bot` | Run `docker compose ps`. The bot must show `healthy`. Confirm that your number is in `contacts` and that the Whapi channel is connected. |
| `docker compose ps` shows the bot restarting | Run `docker compose logs sealwharf` and the check above. The usual causes are a mistake in the configuration or the Cloudflare route from step 5 not created yet. |
| "I could not find a clear place to sign" | The document needs the signer's full name, as in the certificate, next to one empty signature line. The name must appear only once as a signature label. |
| "Could not open certificate" | The password is wrong, or the certificate file is not in `config/certificates/`. |
| "The configured name does not match certificate" | Write `name` with the same words as the holder's name in the certificate. Capitals and accents do not matter, but every word must be there, in the same order. |
| "could not open … token is connected" | The token is unplugged or not attached. On Windows, repeat `usbipd attach`. |
| "refused its PIN" | See *About the PIN* above. |
| A Word document comes back with a different layout | See *Fonts in Word documents* below. |
| The owner did not receive the approval request | Confirm the owner's number in `contacts`, with country and area codes. |

## Fonts in Word documents

A Word document is converted to PDF before it is signed, and the conversion needs the fonts the document was written with. Sealwharf already has:

- free fonts with the same widths as Arial, Times New Roman, Courier New, Calibri, Cambria, Helvetica and Palatino, so documents written with those keep every line in place;
- free fonts common in documents written on the web: Roboto, Open Sans, Lato, Noto, Inter, EB Garamond, Comfortaa and Lobster.

When a document uses a font that is missing, the bot signs it anyway and tells you which font was replaced by which, for example `Aptos → Carlito`. The text is all there, but lines may break in other places.

To get exactly the same result as on your computer, do one of these:

- Send the document as PDF.
- Give Sealwharf the font. Copy its files (`.ttf` or `.otf`) into the folder `config/fonts/`. The next document already uses them; there is nothing to restart. On Windows the fonts are in `C:\Windows\Fonts`. Copy only fonts you have the right to use.

## Good to know

- **Your number stays yours.** The WhatsApp number can still be used for ordinary conversations. The bot only looks at documents and at the exact messages `wake up bot` (or its Portuguese or Spanish form) and `bot status` sent by the numbers in `contacts`, and at the answer to a question it has just asked. Every other message, from anyone, is ignored and never stored.
- **Privacy.** Documents and certificates are processed on your computer. Whapi and Cloudflare only carry the messages. Certificates and passwords are never accepted through the chat: a `.p12` file sent to the bot is refused without being opened.
- **Record of signatures.** Every signature is recorded with the time, who asked, who approved and the fingerprint of the document. Read the record with `docker compose exec sealwharf cat /data/audit.jsonl`.
- **Logs.** `docker compose logs sealwharf` shows what the bot is doing, without phone numbers, names or file names.
- **Expiring certificates.** From 30 days before a certificate expires, its owner gets a daily reminder.
- **Limits.** Each number may send 20 documents per hour, and a question stays open for 5 minutes. Both can be changed under `limits` in the configuration. For someone who sends documents in bulk, add `limit: 200` (documents per hour) to that person in `contacts`.
- **Large batches take a moment.** Documents are signed one after the other, and the bot leaves a second between its messages so that WhatsApp does not take them for spam.
- **Signed documents can be signed again.** Send a PDF that already has signatures to add another signer; the earlier signatures stay valid.
- **Stopping.** `docker compose --profile token down` stops everything. Your configuration and records are kept.

## Advanced settings

These are optional, per certificate. `config/sealwharf.example.yaml` shows the syntax.

| Setting | Use |
| --- | --- |
| `aliases` | Other ways the holder's name is written in documents, such as `["Johnny Doe"]`. Each may use only names that are in the certificate, in the same order. |
| `logo`, `logo_scale` | A PNG or JPEG under `config/logos/` to show in the signature stamp, and its size from `0.1` to `1.0`. |
| `timestamp_url` | Adds a timestamp from an RFC 3161 authority. Needs Internet access while signing. |
| `trust_roots` | Root certificates (PEM or DER files under `config/`). The signer's chain and its revocation status must validate against them, and the proof is embedded in the PDF. For ICP-Brasil, download the roots from the official ITI repository. |
| `visual_review: ollama` | Lets a local AI model find signature places in forms and scanned pages. Start it with `docker compose --profile ai up -d`, download the model once with `docker compose --profile ai exec ollama ollama pull qwen3-vl:4b`, and set `SEALWHARF_OLLAMA_URL=http://ollama:11434` in a `.env` file. |

Notes on how signing works:

- Names are compared without capitals or accents everywhere: in the configuration, in the certificate and in documents.
- Middle names may be written as initials (`John M. Doe` for `John Michael Doe`), but the first and last names must be complete. Spelling errors are not accepted.
- If the place to sign is unclear, or a name could belong to two signers, nothing is signed for that signer.
- A signature proves the document was signed with that certificate and was not changed afterwards. Checking whether the certificate authority is trusted is done only when `trust_roots` is set.

## Development

Python 3.12 with `config/requirements-dev.txt`. The code is under `src/sealwharf/` and the tests under `tests/`; `tests/test_flow.py` covers the whole conversation flow with self-signed certificates.

```bash
python -m pytest -q
```

**Adding a language.** Each language is one file in `src/sealwharf/locales/`, named after its code. Copy `en.toml` to, say, `fr.toml`, translate the values and keep the `{placeholders}` as they are. Nothing else changes: the new code is accepted in `language` and `lang` at once. The bot refuses to start, and the tests fail, while a language file lacks an entry or uses other placeholders, so a language is never half translated.

```bash
python -m ruff check src tests
```

GitHub Actions runs both and builds the image on every push.
