"""Reach the private Ollama service that hosts the local vision model."""

import httpx

from .local_ai import MODEL, ollama_base_url

TAGS_URL = ollama_base_url() + "/api/tags"


def model_status(*, transport: httpx.BaseTransport | None = None) -> bool | None:
    """Return model availability, or None when no service can be reached."""
    try:
        with httpx.Client(timeout=2, trust_env=False, follow_redirects=False, transport=transport) as client:
            response = client.get(TAGS_URL)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ValueError("Unexpected local model response")
        return any(isinstance(item, dict) and item.get("name") == MODEL for item in payload["models"])
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        return None


def ensure_local_model() -> None:
    status = model_status()
    if status is None:
        raise RuntimeError("The local model service is not reachable")
    if status is False:
        raise RuntimeError("The required local visual model is not installed")


class LocalModelSession:
    """Check the model once, when first needed; any failure means no suggestion."""

    def __init__(self) -> None:
        self.ready = False

    def ensure(self) -> None:
        if not self.ready:
            ensure_local_model()
            self.ready = True

    def review_contextual(self, document, signer_name, candidate):
        try:
            self.ensure()
            from .local_ai import review_contextual_candidate
            return review_contextual_candidate(document, signer_name, candidate)
        except (RuntimeError, ValueError, OSError, httpx.HTTPError):
            return None

    def choose_place(self, document, signer_name):
        """Return ("chosen", place), ("none", None), or ("unavailable", None) when the model cannot be asked."""
        try:
            self.ensure()
            from .local_ai import NothingToChoose, choose_signature_place
            try:
                place = choose_signature_place(document, signer_name)
            except NothingToChoose:
                return "unavailable", None
            return ("chosen", place) if place is not None else ("none", None)
        except (RuntimeError, ValueError, OSError, httpx.HTTPError):
            return "unavailable", None

    def suggest_general(self, document, signer_name):
        try:
            self.ensure()
            from .local_ai import suggest_general_position
            return suggest_general_position(document, signer_name)
        except (RuntimeError, ValueError, OSError, httpx.HTTPError):
            return None
