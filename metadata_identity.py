"""Shared, conservative metadata identity rules. No network or task writes."""
import html
import re
import unicodedata

from core import SafetyStop


class MetadataSkip(SafetyStop):
    def __init__(self, note):
        super().__init__(note)
        self.note = note


def norm(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def doi(value):
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", norm(value))
    if value and not re.fullmatch(r"10\.\d{4,9}/[^\s?#]+", value):
        raise MetadataSkip("DOI待核验")
    return value


def text(value):
    if not isinstance(value, str) or len(value) > 16000:
        raise MetadataSkip("元数据格式待核验")
    value = html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
        raise MetadataSkip("元数据格式待核验")
    return value


def legacy_title_key(value):
    """Ignore presentation only, never remove/replace words or fuzzy-match titles."""
    value = norm(text(value)).translate(str.maketrans({
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    }))
    return re.sub(r"\s*-\s*", "-", value).rstrip(" .")


def title_key(value):
    # Legacy PDF ingestion sometimes joins adjacent words. Preserve every
    # letter, digit and punctuation mark; ignore only whitespace presentation.
    return re.sub(r'\s+', '', legacy_title_key(value))


def name_tokens(value):
    return sorted(re.findall(r"[^\W_]+", norm(value), re.UNICODE))
