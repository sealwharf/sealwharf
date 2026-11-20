"""Load the single YAML configuration: tokens, certificates, roles, languages and limits.

Each certificate has one owner, who signs, and optional preparers, who send
documents on the owner's behalf. The file holds every secret the bot needs,
so it must stay private: it is never committed, logged or sent anywhere.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .authorization import canonical_number
from .messages import DEFAULT_LANGUAGE, LANGUAGES, resolve_language

NUMBER = re.compile(r"[0-9]{9,15}")
PUBLIC_URL = re.compile(r"https://[a-z0-9]([a-z0-9.-]{0,250}[a-z0-9])?")
# `whapi_token` is the earlier name of `gateway_token` and is still read.
SETTINGS = ("public_url", "gateway_type", "gateway_url", "gateway_token", "gateway_session", "tunnel_token", "language",
            "contacts", "certificates", "limits", "whapi_token", "timezone")
GATEWAY_TYPES = ("whapi", "waha")  # one adapter each, in adapters/gateways.py
SERVICE_URL = re.compile(r"https?://[A-Za-z0-9]([A-Za-z0-9.-]{0,250}[A-Za-z0-9])?(:[0-9]{1,5})?")
ENTRY_KEYS = frozenset({"id", "name", "file", "password", "owner", "preparers", "trusted", "visual_review", "logo", "logo_scale",
                        "timestamp_url", "trust_roots", "aliases", "token"})


@dataclass(frozen=True)
class SignerConfig:
    id: str
    name: str
    certificate: Path | None  # None when the key lives on a token
    visual_review: str = "deterministic"
    logo: Path | None = None
    logo_scale: float = 1.0
    allowed_senders: frozenset[str] = frozenset()
    timestamp_url: str | None = None
    trust_roots: tuple[Path, ...] = ()
    # The owner confirms requests from preparers that are not auto-trusted.
    # Without an owner every allowed sender is trusted.
    owner: str | None = None
    autotrust: frozenset[str] = frozenset()
    password: str | None = field(default=None, repr=False)
    # Other ways the holder's name is written in documents.
    aliases: tuple[str, ...] = ()
    token_module: PurePosixPath | None = None
    token_label: str | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def needs_confirmation(self, sender: str) -> bool:
        return self.owner is not None and sender != self.owner and sender not in self.autotrust


@dataclass(frozen=True)
class Contact:
    number: str
    name: str | None = None
    lang: str = DEFAULT_LANGUAGE
    limit: int | None = None


@dataclass(frozen=True)
class AppConfig:
    signers: tuple[SignerConfig, ...]
    languages: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    requests_per_hour: int = 20
    confirmation_minutes: int = 5
    # The fixed HTTPS address the tunnel publishes, and the tokens of the two services.
    # The loader accepts a file without them; the bot refuses to start without them.
    public_url: str | None = None
    gateway_token: str | None = field(default=None, repr=False)
    tunnel_token: str | None = field(default=None, repr=False)
    # Contacts with an hourly limit of their own, for people who send documents in bulk.
    request_limits: dict[str, int] = field(default_factory=dict)
    # How many days the record of signatures is kept.
    log_days: int = 90
    # The time zone of the times people read, such as America/Sao_Paulo; None for the time zone of the server.
    timezone: str | None = None
    # The WhatsApp gateway: which adapter talks to it and, for one you host, where it is and its session name.
    gateway_url: str | None = None
    gateway_type: str = "whapi"
    gateway_session: str = "default"

    @property
    def senders(self) -> frozenset[str]:
        return frozenset(number for signer in self.signers for number in signer.allowed_senders)


def _name_words(name: str) -> tuple[str, ...]:
    plain = "".join(c for c in unicodedata.normalize("NFKD", name.casefold()) if not unicodedata.combining(c))
    return tuple(re.findall(r"[^\W\d_]+", plain))


def load_config(path: Path) -> AppConfig:
    """Load and validate the whole configuration, refusing ambiguous identities."""
    try:
        with path.open("rb") as stream:
            config = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("Could not read the configuration file.") from exc
    if not isinstance(config, dict) or set(config) - set(SETTINGS):
        raise ValueError("The configuration may contain only " + ", ".join(SETTINGS[:-2]) + " and " + SETTINGS[-2] + ".")
    default_language = resolve_language(str(config.get("language", DEFAULT_LANGUAGE)))
    if default_language is None:
        raise ValueError("language must be one of: " + ", ".join(LANGUAGES) + ".")
    tokens = {}
    for key in ("gateway_token", "whapi_token", "tunnel_token"):
        value = config.get(key)
        if value is not None and (not isinstance(value, str) or not value or len(value) > 2000 or any(c.isspace() for c in value)):
            raise ValueError(f"{key} must be the token as text, in quotes and without spaces.")
        tokens[key] = value
    public_url = config.get("public_url")
    if public_url is not None and (not isinstance(public_url, str) or not PUBLIC_URL.fullmatch(public_url)):
        raise ValueError("public_url must be the bot's HTTPS address, such as https://bot.example.com, with nothing after it.")
    if tokens["gateway_token"] is not None and tokens["whapi_token"] is not None:
        raise ValueError("Use gateway_token alone; whapi_token is its earlier name.")
    gateway_token = tokens["gateway_token"] or tokens["whapi_token"]
    gateway_type = config.get("gateway_type", AppConfig.gateway_type)
    if gateway_type not in GATEWAY_TYPES:
        raise ValueError("gateway_type must be one of: " + ", ".join(GATEWAY_TYPES) + ".")
    gateway_session = config.get("gateway_session", AppConfig.gateway_session)
    if not isinstance(gateway_session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,54}", gateway_session):
        raise ValueError("gateway_session must be a short name of letters, digits, underscores or hyphens.")
    gateway_url = config.get("gateway_url")
    if gateway_url is None and gateway_type != "whapi":
        raise ValueError(f"gateway_url is missing: a {gateway_type} gateway needs its address.")
    if gateway_url is not None and (not isinstance(gateway_url, str) or not SERVICE_URL.fullmatch(gateway_url)):
        raise ValueError("gateway_url must be the address of the WhatsApp gateway, such as http://host.docker.internal:8787, "
                         "with nothing after it.")
    entries = config.get("certificates")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 50 or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Configure between one and fifty certificates.")
    contacts = _parse_contacts(config.get("contacts"), default_language)
    languages = {contact.number: contact.lang for contact in contacts.values()}
    contact_names = {contact.number: contact.name for contact in contacts.values() if contact.name}
    signers = tuple(_parse_signer(entry, path.resolve().parent, contacts) for entry in entries)
    identifiers = [signer.id.casefold() for signer in signers]
    names = [_name_words(signer.name) for signer in signers]
    names += [_name_words(alias) for signer in signers for alias in signer.aliases]
    files = [signer.certificate for signer in signers if signer.certificate is not None]
    if len(set(identifiers)) != len(signers) or len(set(names)) != len(names) or len(set(files)) != len(files):
        raise ValueError("Certificate IDs, names, aliases and files must be distinct.")
    limits = config.get("limits", {})
    if not isinstance(limits, dict) or set(limits) - {"requests_per_hour", "confirmation_minutes", "log_days"}:
        raise ValueError("Limits may contain only requests_per_hour, confirmation_minutes and log_days.")
    log_days = limits.get("log_days", 90)
    if isinstance(log_days, bool) or not isinstance(log_days, int) or not 1 <= log_days <= 3650:
        raise ValueError("log_days must be a whole number of days, from 1 to 3650.")
    per_hour, minutes = limits.get("requests_per_hour", 20), limits.get("confirmation_minutes", 5)
    if (isinstance(per_hour, bool) or not isinstance(per_hour, int) or not 1 <= per_hour <= 1000
            or isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 60):
        raise ValueError("Limits must be whole numbers: 1-1000 requests per hour and 1-60 confirmation minutes.")
    request_limits = {contact.number: contact.limit for contact in contacts.values() if contact.limit is not None}
    timezone = config.get("timezone")
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except (TypeError, ValueError, OSError, ZoneInfoNotFoundError):
            raise ValueError("timezone must be the name of a time zone, such as America/Sao_Paulo.") from None
    return AppConfig(signers, languages, contact_names, per_hour, minutes, public_url, gateway_token, tokens["tunnel_token"],
                     request_limits, gateway_url=gateway_url, gateway_type=gateway_type, gateway_session=gateway_session,
                     log_days=log_days, timezone=timezone)


def _parse_contacts(value: object, default_language: str) -> dict[str, Contact]:
    """Read the people who may talk to the bot; certificates refer to them by id."""
    if not isinstance(value, dict) or not 1 <= len(value) <= 200:
        raise ValueError("Add a contacts section with at least one person.")
    contacts: dict[str, Contact] = {}
    for identifier, person in value.items():
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", identifier):
            raise ValueError("Each contact needs a short id of letters, digits, underscores or hyphens.")
        if (not isinstance(person, dict) or set(person) - {"name", "number", "lang", "limit"}
                or not isinstance(person.get("number"), str) or not NUMBER.fullmatch(person["number"])):
            raise ValueError(f"Contact {identifier} needs its number in quotes, with country and area codes and digits only.")
        name = person.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 80 or "\n" in name):
            raise ValueError(f"The name of contact {identifier} must be a short text.")
        language = resolve_language(str(person.get("lang", default_language)))
        if language is None:
            raise ValueError(f"The lang of contact {identifier} must be one of: " + ", ".join(LANGUAGES) + ".")
        number = canonical_number(person["number"])
        if any(other.number == number for other in contacts.values()):
            raise ValueError(f"Contact {identifier} repeats the number of another contact.")
        limit = person.get("limit")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000):
            raise ValueError(f"The limit of contact {identifier} must be a whole number of documents per hour, from 1 to 5000.")
        contacts[identifier] = Contact(number, name.strip() if name else None, language, limit)
    return contacts


def _as_list(value: object) -> object:
    """Accept one item written alone where a list is expected, as in `preparers: jane`."""
    return [value] if isinstance(value, str) else value


def _members(entry: dict, key: str, contacts: dict[str, Contact]) -> list[str]:
    """Turn a list of contact ids in a certificate entry into their numbers."""
    value = _as_list(entry.get(key, []))
    if not isinstance(value, list) or len(value) > 100 or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of contact ids.")
    missing = [item for item in value if item not in contacts]
    if missing:
        raise ValueError(f"Unknown contact in {key}: {missing[0]}. Add it to the contacts section first.")
    return [contacts[item].number for item in value]


def _resolve(value: object, base_dir: Path, suffixes: set[str], message: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    if path.suffix.lower() not in suffixes or not path.is_file():
        raise ValueError(message)
    return path.resolve()


def _parse_signer(entry: dict, base_dir: Path, contacts: dict[str, Contact]) -> SignerConfig:
    if set(entry) - ENTRY_KEYS:
        raise ValueError("A certificate entry has an unknown setting.")
    identifier, name = entry.get("id"), entry.get("name")
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", identifier):
        raise ValueError("Certificate ID must be a short alias of letters, digits, underscores or hyphens.")
    if not isinstance(name, str) or len(name) > 200 or len(re.findall(r"[^\W\d_]+", name, re.UNICODE)) < 2:
        raise ValueError("The name on the stamp must contain at least two words.")
    password = entry.get("password")
    if not isinstance(password, str) or not password or len(password) > 200 or "\n" in password:
        # A bare number would lose leading zeros, so text in quotes is required.
        raise ValueError(f"Certificate {identifier} needs its password or PIN in quotes.")
    owner = None
    if entry.get("owner") is not None:
        if not isinstance(entry["owner"], str):
            raise ValueError(f"The owner of certificate {identifier} must be one contact id.")
        owner = _members({"owner": [entry["owner"]]}, "owner", contacts)[0]
    preparers, trusted = _members(entry, "preparers", contacts), _members(entry, "trusted", contacts)
    numbers = [number for number in (owner, *preparers, *trusted) if number]
    if len(set(numbers)) != len(numbers):
        raise ValueError(f"A contact appears more than once in certificate {identifier}.")
    autotrust = set(trusted)
    if not numbers or (owner is None and preparers):
        raise ValueError(f"Certificate {identifier} needs an owner, or only trusted contacts when nobody is there to approve.")
    aliases = _as_list(entry.get("aliases", []))
    if (not isinstance(aliases, list) or len(aliases) > 5 or any(
            not isinstance(alias, str) or len(alias) > 200 or len(re.findall(r"[^\W\d_]+", alias, re.UNICODE)) < 2 for alias in aliases)):
        raise ValueError("Aliases must be up to five other spellings of the name, each with at least two words.")
    visual_review = entry.get("visual_review", "deterministic")
    if visual_review not in ("deterministic", "ollama"):
        raise ValueError("Visual review must be deterministic or ollama.")
    token = entry.get("token")
    certificate = token_module = token_label = None
    if token is None:
        certificate = _resolve(entry.get("file"), base_dir, {".p12", ".pfx"}, "The configured P12 or PFX certificate was not found.")
    else:
        if "file" in entry or not isinstance(token, dict) or set(token) - {"module", "label"}:
            raise ValueError("Use either a certificate file or a token with module and optional label.")
        module, token_label = token.get("module"), token.get("label")
        if not isinstance(module, str) or not module.startswith("/") or len(module) > 300:
            raise ValueError("Token module must be the absolute path of the PKCS#11 library inside the container.")
        if token_label is not None and (not isinstance(token_label, str) or not 1 <= len(token_label) <= 32):
            raise ValueError("Token label must have between 1 and 32 characters.")
        token_module = PurePosixPath(module)
    logo_scale = entry.get("logo_scale", 1.0)
    if isinstance(logo_scale, bool) or not isinstance(logo_scale, (int, float)) or not 0.1 <= logo_scale <= 1.0:
        raise ValueError("Logo scale must be between 0.1 and 1.0.")
    logo_path = None
    if entry.get("logo") is not None:
        logo_path = _resolve(entry["logo"], base_dir, {".png", ".jpg", ".jpeg"}, "Logo must be a PNG or JPEG under the logos folder.")
        if not logo_path.is_relative_to((base_dir / "logos").resolve()):
            raise ValueError("Logo must be a PNG or JPEG under the logos folder.")
        from .documents.appearance import signature_style
        signature_style(logo_path, logo_scale)
    timestamp_url = entry.get("timestamp_url")
    if timestamp_url is not None and (not isinstance(timestamp_url, str) or len(timestamp_url) > 500
                                      or not re.fullmatch(r"https?://[^\s/@]+(/\S*)?", timestamp_url)):
        raise ValueError("Timestamp URL must be an http or https address without credentials.")
    roots = entry.get("trust_roots", [])
    if not isinstance(roots, list) or len(roots) > 20:
        raise ValueError("Trust roots must be a list of certificate file paths.")
    root_paths = tuple(_resolve(root, base_dir, {".pem", ".crt", ".cer"}, "A configured trust root certificate was not found.")
                       for root in roots)
    return SignerConfig(identifier, name.strip(), certificate, visual_review, logo_path, float(logo_scale),
                        frozenset(numbers), timestamp_url, root_paths, owner, frozenset(autotrust), password,
                        tuple(alias.strip() for alias in aliases), token_module, token_label)
