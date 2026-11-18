"""Compact two-column appearance for a cryptographic PDF signature."""

from dataclasses import dataclass, replace
from pathlib import Path
from textwrap import wrap

from PIL import Image, UnidentifiedImageError
from pyhanko.pdf_utils import layout
from pyhanko.pdf_utils.font.basic import SimpleFontEngineFactory
from pyhanko.pdf_utils.images import PdfImage
from pyhanko.pdf_utils.text import TextBox, TextBoxStyle
from pyhanko.stamp import TextStamp, TextStampStyle


def _lines(value: str, width: int) -> str:
    return "\n".join(wrap(" ".join(value.split()), width=width) or [""])


class SignatureStamp(TextStamp):
    """Render the certificate name and signing details without a frame."""

    def _render_background(self):
        scale = self.style.logo_scale
        x = self.box.width * (1 - scale) / 2
        y = self.box.height * (1 - scale) / 2
        return b"q %g 0 0 %g %g %g cm " % (scale, scale, x, y) + super()._render_background() + b" Q"

    def _inner_layout_natural_size(self):
        params = self.get_default_text_params()
        params.update(self.text_params or {})
        name = str(params.get("signer", "Unknown signer"))
        no_margin = layout.SimpleBoxLayoutRule(
            x_align=layout.AxisAlignment.ALIGN_MID, y_align=layout.AxisAlignment.ALIGN_MID,
            margins=layout.Margins.uniform(0),
        )
        title = TextBox(
            TextBoxStyle(font=SimpleFontEngineFactory("Helvetica", 0.65), font_size=13,
                         leading=15, box_layout_rule=no_margin),
            writer=self.writer, resources=self.resources, font_name="NameFont",
        )
        details = TextBox(
            TextBoxStyle(font=SimpleFontEngineFactory("Helvetica", 0.65), font_size=10,
                         leading=12, box_layout_rule=no_margin),
            writer=self.writer, resources=self.resources, font_name="DetailsFont",
        )
        compact = self.box.width <= 200 and self.box.height >= 28
        title.content = _lines(name, 16 if compact else 22)
        if compact:
            date, clock, offset = params["ts"].split()
            details.content = f"Digitally signed\nDate: {date}\n{clock} {offset}"
        else:
            details.content = "Digitally signed by\n" + _lines(name, 31) + "\nDate: " + params["ts"]
        title_commands = title.render()
        details_commands = details.render()
        height = max(title.box.height, details.box.height)
        gap = 8
        width = title.box.width + gap + details.box.width
        commands = [
            b"q 1 0 0 1 0 %g cm " % ((height - title.box.height) / 2) + title_commands + b" Q",
            b"q 1 0 0 1 %g %g cm " % (title.box.width + gap, (height - details.box.height) / 2)
            + details_commands + b" Q",
        ]
        return commands, (width, height)


@dataclass(frozen=True)
class SignatureStampStyle(TextStampStyle):
    logo_scale: float = 1.0

    def create_stamp(self, writer, box, text_params):
        return SignatureStamp(writer=writer, style=self, box=box, text_params=text_params)


SIGNATURE_STYLE = SignatureStampStyle(
    border_width=0,
    background=None,
    timestamp_format="%Y.%m.%d %H:%M %z",
    inner_content_layout=layout.SimpleBoxLayoutRule(
        x_align=layout.AxisAlignment.ALIGN_MID,
        y_align=layout.AxisAlignment.ALIGN_MID,
        margins=layout.Margins.uniform(2),
    ),
)


def signature_style(logo: Path | None = None, logo_scale: float = 1.0) -> SignatureStampStyle:
    """Load a bounded local logo and use it as a subtle signature background."""
    if isinstance(logo_scale, bool) or not isinstance(logo_scale, (int, float)) or not 0.1 <= logo_scale <= 1.0:
        raise ValueError("Logo scale must be between 0.1 and 1.0.")
    path = logo or Path(__file__).resolve().parents[1] / "assets" / "default-logo.png"
    try:
        if path.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("Logo exceeds the size limit.")
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG"} or image.width * image.height > 4_000_000:
                raise ValueError("Logo must be a PNG or JPEG of at most four megapixels.")
            image.load()
            clean_image = image.convert("RGBA")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError("Could not read the configured logo.") from exc
    return replace(SIGNATURE_STYLE, logo_scale=float(logo_scale), background=PdfImage(clean_image), background_opacity=0.25,
                   background_layout=layout.SimpleBoxLayoutRule(
                       x_align=layout.AxisAlignment.ALIGN_MID, y_align=layout.AxisAlignment.ALIGN_MID,
                       margins=layout.Margins.uniform(2)))
