"""Internationalization support for MDP2P client."""

import json
from pathlib import Path

_LOCALES_DIR = Path(__file__).parent / "locales"
_translations: dict[str, str] = {}
_current_lang: str = "fr"


def load_language(lang: str) -> None:
    """Load translations for the given language code."""
    global _translations, _current_lang
    path = _LOCALES_DIR / f"{lang}.json"
    if not path.exists():
        path = _LOCALES_DIR / "en.json"
        lang = "en"
    with open(path, encoding="utf-8") as f:
        _translations = json.load(f)
    _current_lang = lang


def t(key: str, **kwargs) -> str:
    """Get translated string by key, with optional format variables."""
    text = _translations.get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text


def current_language() -> str:
    """Return the current language code."""
    return _current_lang
