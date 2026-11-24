"""Peso and meter-reading parsing/formatting. Money is integer centavos; readings are Decimal."""
import re
from decimal import Decimal, ROUND_HALF_UP

from . import UserError

MAX_PESOS = 100_000_000
MAX_READING_DIGITS = 9
PESO_PATTERN = re.compile(r'(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?')
READING_PATTERN = re.compile(rf'[0-9]{{1,{MAX_READING_DIGITS}}}(?:\.[0-9]{{1,2}})?')


def parse_pesos(text):
    """'1,050.00', '₱1050', 'PHP 1050.5' -> centavos. Rejects malformed comma groups."""
    value = re.sub(r'^(?:₱|PHP)\s*', '', str(text).strip(), flags=re.IGNORECASE)
    if len(value) > 32 or not PESO_PATTERN.fullmatch(value):
        raise UserError('Enter an amount like 1050 or 1,050.00, with up to two decimal places.')
    amount = Decimal(value.replace(',', ''))
    if amount > MAX_PESOS:
        raise UserError('That amount is too large.')
    return int(amount * 100)


def format_pesos(cents):
    """Centavos -> '₱1,050.00' (negative: '-₱50.00')."""
    sign = '-' if cents < 0 else ''
    whole, fraction = divmod(abs(cents), 100)
    return f'{sign}₱{whole:,}.{fraction:02d}'


_ONES = ('', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten', 'Eleven', 'Twelve',
         'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen')
_TENS = ('', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety')
_GROUPS = ((1_000_000_000, 'Billion'), (1_000_000, 'Million'), (1_000, 'Thousand'), (1, ''))


def pesos_in_words(cents):
    """Centavos -> the amount as the office writes it on receipts: 112304 -> 'One Thousand One Hundred Twenty-Three
    Pesos & 04/100'; whole pesos have no centavo part (80000 -> 'Eight Hundred Pesos')."""
    if cents < 0:
        raise ValueError('A receipt amount is never negative.')
    whole, fraction = divmod(cents, 100)
    parts = []
    for size, name in _GROUPS:
        group, whole = divmod(whole, size)
        if group:
            parts.append(_under_thousand(group) + (f' {name}' if name else ''))
    words = ' '.join(parts) or 'Zero'
    pesos = f"{words} Peso{'' if cents // 100 == 1 else 's'}"
    return f'{pesos} & {fraction:02d}/100' if fraction else pesos


def _under_thousand(number):
    hundreds, rest = divmod(number, 100)
    words = [f'{_ONES[hundreds]} Hundred'] if hundreds else []
    if rest >= 20:
        tens, ones = divmod(rest, 10)
        words.append(_TENS[tens] + (f'-{_ONES[ones]}' if ones else ''))
    elif rest:
        words.append(_ONES[rest])
    return ' '.join(words)


def parse_reading(text):
    """Meter reading: non-negative, at most two decimals, plain digits only (no 1e3, NaN, signs)."""
    value = str(text).strip()
    if not READING_PATTERN.fullmatch(value):
        raise UserError('Enter a reading using digits only, with at most two decimal places.')
    return Decimal(value)


def format_reading(value):
    """Canonical stored text for a reading ('784', '10.50')."""
    return f'{value:f}'


def round_half_up(value):
    """Decimal -> int, half-up (the one rounding step per bill line, applied to centavos)."""
    return int(value.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
