"""Parse a 13F-HR information-table XML into holding dicts.

Namespaced XML (http://www.sec.gov/edgar/document/thirteenf/informationtable).
<value> is treated as WHOLE DOLLARS (SEC standard since 2023).  Never raises on
hostile/empty input -> returns [] (parity with the form4/sc13 parsers).
"""
from __future__ import annotations
import xml.etree.ElementTree as ET
import structlog

log = structlog.get_logger(__name__)


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_text(el, local: str) -> str | None:
    for child in el.iter():
        if _localname(child.tag) == local and child.text:
            return child.text.strip()
    return None


def parse_form13f_infotable(xml: str) -> list[dict]:
    if not xml or not xml.strip():
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        log.warning("form13f.parse_error")
        return []
    out: list[dict] = []
    for el in root.iter():
        if _localname(el.tag) != "infoTable":
            continue
        cusip = _find_text(el, "cusip")
        if not cusip:
            continue
        try:
            value_usd = float(_find_text(el, "value") or 0)
            shares = float(_find_text(el, "sshPrnamt") or 0)
        except ValueError:
            continue
        out.append({
            "cusip": cusip.strip().upper(),
            "issuer_name": (_find_text(el, "nameOfIssuer") or "").strip(),
            "value_usd": value_usd,
            "shares": shares,
            "put_call": (_find_text(el, "putCall") or None),
        })
    return out
