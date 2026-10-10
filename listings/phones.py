"""
Phone numbers as Google gives them ("+971 4 345 1200", "+971 50 123 4567").
"""
import re

from .countries import MOBILE_PREFIXES

NO_TRUNK_ZERO = ("600", "700", "800", "9200")


def national(phone, country):
    """Digits after the country code or the leading 0: "43451200"."""
    digits = re.sub(r"\D", "", phone or "")
    if phone.strip().startswith("00"):
        digits = digits[2:]
    if country.phone_code and digits.startswith(country.phone_code) and (
            phone.strip().startswith(("+", "00")) or len(digits) > 10):
        return digits[len(country.phone_code):]
    return digits[1:] if digits.startswith("0") else digits


def local(phone, country):
    """As dialled in the country: "+971 4 345 1200" -> "04 345 1200"."""
    if not phone or not country.phone_code:
        return phone
    prefix = f"+{country.phone_code} "
    if not phone.startswith(prefix):
        return phone
    rest = phone[len(prefix):]
    # Shared and toll-free numbers (600, 800, 9200...) have no leading 0
    return rest if rest.startswith(NO_TRUNK_ZERO) else "0" + rest


def tel_url(phone, country):
    digits = national(phone, country)
    if not digits:
        return ""
    return f"tel:+{country.phone_code}{digits}" if country.phone_code else f"tel:{digits}"


def whatsapp_url(phone, country):
    """wa.me link for mobile numbers; "" for landlines."""
    digits = national(phone, country)
    if digits and country.phone_code and digits.startswith(MOBILE_PREFIXES.get(country.code, ())) \
            and 7 <= len(digits) <= 10:
        return f"https://wa.me/{country.phone_code}{digits}"
    return ""
