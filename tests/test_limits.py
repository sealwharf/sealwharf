from io import BytesIO

import pytest
from pypdf import PdfWriter

from sealwharf import limits
from sealwharf.documents.validation import DocumentError, validate_pdf
from sealwharf.signer_config import load_config


def pdf(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def config(tmp_path):
    (tmp_path / "certificate.p12").write_bytes(b"synthetic")
    path = tmp_path / "sealwharf.yaml"
    body = ('contacts:\n  jane:\n    number: "5511999999999"\ncertificates:\n  - id: one\n    name: Jane Example\n'
            '    file: certificate.p12\n    password: "synthetic"\n    owner: jane\n')
    return lambda extra="": (path.write_text(body + extra, encoding="utf-8"), load_config(path))[1]


def test_every_limit_has_a_default_and_can_be_chosen(config):
    assert config().limits == limits.Limits()
    chosen = config("limits:\n  max_pages: 3\n  model_seconds: 900\n  waiting_per_owner: 50\n  requests_per_hour: 500\n").limits
    assert (chosen.max_pages, chosen.model_seconds, chosen.waiting_per_owner) == (3, 900, 50)
    assert config("limits:\n  requests_per_hour: 500\n").requests_per_hour == 500


@pytest.mark.parametrize("wrong", ["limits:\n  max_pages: 0\n", "limits:\n  model_seconds: fast\n",
                                   "limits:\n  max_file_mb: true\n", "limits:\n  pages: 3\n", "limits: 5\n"])
def test_a_limit_out_of_range_or_unknown_is_refused(config, wrong):
    with pytest.raises(ValueError, match="limits"):
        config(wrong)


def test_the_chosen_limits_apply_to_every_document(monkeypatch):
    monkeypatch.setattr(limits, "current", limits.Limits())
    validate_pdf(pdf(3))
    limits.use(limits.parse({"max_pages": 2}))
    with pytest.raises(DocumentError):
        validate_pdf(pdf(3))
    limits.use(limits.parse({"max_file_mb": 1}))
    with pytest.raises(DocumentError):
        validate_pdf(b"%PDF-1.7" + b" " * (1024 * 1024))
