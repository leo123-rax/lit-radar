"""Guess the language of a title from its writing system. No dependencies, no network.

Regional sources (J-STAGE, KCI, SciELO) carry titles in Japanese, Korean, Portuguese, Spanish or
English, and some tell us the language explicitly while others do not. When a source does not say,
the script is a reliable proxy for the CJK cases; Latin-script text falls back to the caller's
default because Portuguese / Spanish / English cannot be told apart safely from a title alone.
"""
from __future__ import annotations

import re

HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
KANA = re.compile(r"[぀-ヿㇰ-ㇿｦ-ﾟ]")
HAN = re.compile(r"[一-鿿㐀-䶿豈-﫿]")
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
ARABIC = re.compile(r"[؀-ۿ]")
THAI = re.compile(r"[฀-๿]")


def detect_script_lang(text: str, default: str = "en", han: str = "zh") -> str:
    """Return an ISO 639-1 code guessed from the script of `text`.

    `han` is returned for text that contains Han ideographs but no kana: that is Chinese in general,
    but a Japanese source should pass han="ja" because kanji-only Japanese titles are common.
    """
    if not text:
        return default
    if HANGUL.search(text):
        return "ko"
    if KANA.search(text):
        return "ja"
    if HAN.search(text):
        return han
    if CYRILLIC.search(text):
        return "ru"
    if ARABIC.search(text):
        return "ar"
    if THAI.search(text):
        return "th"
    return default
