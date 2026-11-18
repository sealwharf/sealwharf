"""Recognize the WhatsApp numbers that may use the bot."""

import re


def canonical_number(number: str) -> str:
    """Return the one form used to compare numbers.

    WhatsApp identifies many Brazilian mobiles without the ninth digit that
    people write, so `55 53 9xxxx-xxxx` and `55 53 xxxx-xxxx` are the same
    phone. Both are compared in the written, thirteen-digit form.
    """
    if len(number) == 12 and number.startswith("55") and number[4] in "6789":
        return number[:4] + "9" + number[4:]
    return number


def format_number(number: str) -> str:
    """Write a number the way people read it, such as +55 53 99999-9999."""
    if number.startswith("55") and len(number) in (12, 13):
        return f"+55 {number[2:4]} {number[4:-4]}-{number[-4:]}"
    return "+" + number


def parse_senders(value: str) -> frozenset[str]:
    numbers = [number.strip() for number in value.split(",")]
    if not numbers or any(not re.fullmatch(r"[0-9]{9,15}", number) for number in numbers):
        raise ValueError("Configure authorized sender numbers with country codes and digits only, separated by commas.")
    return frozenset(canonical_number(number) for number in numbers)
