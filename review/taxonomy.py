"""Closed taxonomy of content categories used for reviewer/content matching.

Both publishers (on their manifests) and reviewers (in their opt-in
records) pick from the same fixed list of slugs, so matching is a simple
set intersection — no fuzzy string comparison, no synonym tables, and
no surprises for non-technical users.

The canonical identifier is always the slug (ASCII, snake_case). Labels
are localized per language; the slug never changes, so translations can
evolve without invalidating stored records or signed manifests.

Adding a slug is a minor backwards-compatible operation: old peers that
don't know the slug will refuse signatures that include it, so the
taxonomy evolves on a "lowest-version-in-the-network" pace. Removing a
slug is forbidden — it would retroactively invalidate signed content.
"""

from __future__ import annotations


# Canonical order — also the order items are presented in UI pickers.
# Slugs are stable ASCII identifiers; do not rename or remove.
CATEGORY_SLUGS: tuple[str, ...] = (
    # Sciences
    "math",
    "physics",
    "chemistry",
    "biology",
    "astronomy",
    "geosciences",
    # Technologie
    "computing",
    "networks",
    "cybersecurity",
    "ai_ml",
    "electronics_robotics",
    # Santé
    "medicine",
    "nutrition",
    "mental_health",
    # Société
    "politics",
    "economics",
    "law",
    "history",
    "geography",
    # Culture
    "literature",
    "music",
    "cinema",
    "visual_arts",
    # Humanités
    "philosophy_religion",
    "education",
    "languages_linguistics",
    # Nature et vie quotidienne
    "environment_ecology",
    "agriculture_food",
    "leisure_travel",
    # Fallback
    "other",
)

_CATEGORY_SET = frozenset(CATEGORY_SLUGS)


# ─── Localized labels ─────────────────────────────────────────────────


_LABELS_FR: dict[str, str] = {
    "math": "Mathématiques",
    "physics": "Physique",
    "chemistry": "Chimie",
    "biology": "Biologie",
    "astronomy": "Astronomie",
    "geosciences": "Géosciences",
    "computing": "Informatique",
    "networks": "Réseaux et Internet",
    "cybersecurity": "Cybersécurité",
    "ai_ml": "Intelligence artificielle",
    "electronics_robotics": "Électronique et robotique",
    "medicine": "Médecine",
    "nutrition": "Nutrition",
    "mental_health": "Santé mentale",
    "politics": "Politique",
    "economics": "Économie",
    "law": "Droit",
    "history": "Histoire",
    "geography": "Géographie",
    "literature": "Littérature",
    "music": "Musique",
    "cinema": "Cinéma et audiovisuel",
    "visual_arts": "Arts visuels",
    "philosophy_religion": "Philosophie et religion",
    "education": "Éducation et pédagogie",
    "languages_linguistics": "Langues et linguistique",
    "environment_ecology": "Environnement et écologie",
    "agriculture_food": "Agriculture et alimentation",
    "leisure_travel": "Loisirs et voyages",
    "other": "Autre",
}

_LABELS_EN: dict[str, str] = {
    "math": "Mathematics",
    "physics": "Physics",
    "chemistry": "Chemistry",
    "biology": "Biology",
    "astronomy": "Astronomy",
    "geosciences": "Earth sciences",
    "computing": "Computing",
    "networks": "Networks and Internet",
    "cybersecurity": "Cybersecurity",
    "ai_ml": "Artificial intelligence",
    "electronics_robotics": "Electronics and robotics",
    "medicine": "Medicine",
    "nutrition": "Nutrition",
    "mental_health": "Mental health",
    "politics": "Politics",
    "economics": "Economics",
    "law": "Law",
    "history": "History",
    "geography": "Geography",
    "literature": "Literature",
    "music": "Music",
    "cinema": "Cinema and audiovisual",
    "visual_arts": "Visual arts",
    "philosophy_religion": "Philosophy and religion",
    "education": "Education and pedagogy",
    "languages_linguistics": "Languages and linguistics",
    "environment_ecology": "Environment and ecology",
    "agriculture_food": "Agriculture and food",
    "leisure_travel": "Leisure and travel",
    "other": "Other",
}

_LABELS_ZH: dict[str, str] = {
    "math": "数学",
    "physics": "物理",
    "chemistry": "化学",
    "biology": "生物学",
    "astronomy": "天文学",
    "geosciences": "地球科学",
    "computing": "计算机",
    "networks": "网络与互联网",
    "cybersecurity": "网络安全",
    "ai_ml": "人工智能",
    "electronics_robotics": "电子与机器人",
    "medicine": "医学",
    "nutrition": "营养",
    "mental_health": "心理健康",
    "politics": "政治",
    "economics": "经济",
    "law": "法律",
    "history": "历史",
    "geography": "地理",
    "literature": "文学",
    "music": "音乐",
    "cinema": "电影与影视",
    "visual_arts": "视觉艺术",
    "philosophy_religion": "哲学与宗教",
    "education": "教育",
    "languages_linguistics": "语言学",
    "environment_ecology": "环境与生态",
    "agriculture_food": "农业与食物",
    "leisure_travel": "休闲与旅游",
    "other": "其他",
}

_LABELS_AR: dict[str, str] = {
    "math": "الرياضيات",
    "physics": "الفيزياء",
    "chemistry": "الكيمياء",
    "biology": "علم الأحياء",
    "astronomy": "علم الفلك",
    "geosciences": "علوم الأرض",
    "computing": "الحوسبة",
    "networks": "الشبكات والإنترنت",
    "cybersecurity": "الأمن السيبراني",
    "ai_ml": "الذكاء الاصطناعي",
    "electronics_robotics": "الإلكترونيات والروبوتات",
    "medicine": "الطب",
    "nutrition": "التغذية",
    "mental_health": "الصحة النفسية",
    "politics": "السياسة",
    "economics": "الاقتصاد",
    "law": "القانون",
    "history": "التاريخ",
    "geography": "الجغرافيا",
    "literature": "الأدب",
    "music": "الموسيقى",
    "cinema": "السينما والسمعيات البصرية",
    "visual_arts": "الفنون البصرية",
    "philosophy_religion": "الفلسفة والدين",
    "education": "التعليم وعلوم التربية",
    "languages_linguistics": "اللغات واللسانيات",
    "environment_ecology": "البيئة والنظم الإيكولوجية",
    "agriculture_food": "الزراعة والغذاء",
    "leisure_travel": "الترفيه والسفر",
    "other": "أخرى",
}

_LABELS_HI: dict[str, str] = {
    "math": "गणित",
    "physics": "भौतिकी",
    "chemistry": "रसायन विज्ञान",
    "biology": "जीव विज्ञान",
    "astronomy": "खगोल विज्ञान",
    "geosciences": "पृथ्वी विज्ञान",
    "computing": "कंप्यूटिंग",
    "networks": "नेटवर्क और इंटरनेट",
    "cybersecurity": "साइबर सुरक्षा",
    "ai_ml": "कृत्रिम बुद्धिमत्ता",
    "electronics_robotics": "इलेक्ट्रॉनिक्स और रोबोटिक्स",
    "medicine": "चिकित्सा",
    "nutrition": "पोषण",
    "mental_health": "मानसिक स्वास्थ्य",
    "politics": "राजनीति",
    "economics": "अर्थशास्त्र",
    "law": "विधि",
    "history": "इतिहास",
    "geography": "भूगोल",
    "literature": "साहित्य",
    "music": "संगीत",
    "cinema": "सिनेमा और दृश्य-श्रव्य",
    "visual_arts": "दृश्य कला",
    "philosophy_religion": "दर्शन और धर्म",
    "education": "शिक्षा और शिक्षाशास्त्र",
    "languages_linguistics": "भाषाएँ और भाषाविज्ञान",
    "environment_ecology": "पर्यावरण और पारिस्थितिकी",
    "agriculture_food": "कृषि और भोजन",
    "leisure_travel": "अवकाश और यात्रा",
    "other": "अन्य",
}

_LABELS_BY_LANG: dict[str, dict[str, str]] = {
    "fr": _LABELS_FR,
    "en": _LABELS_EN,
    "zh": _LABELS_ZH,
    "ar": _LABELS_AR,
    "hi": _LABELS_HI,
}


# ─── Public API ───────────────────────────────────────────────────────


def is_valid_slug(slug: str) -> bool:
    """True if ``slug`` is a known category identifier."""
    return slug in _CATEGORY_SET


def validate_categories(slugs: list[str]) -> None:
    """Raise ValueError if any entry in ``slugs`` is not a valid category."""
    unknown = [s for s in slugs if s not in _CATEGORY_SET]
    if unknown:
        raise ValueError(
            f"unknown categor(y/ies): {', '.join(sorted(set(unknown)))}. "
            f"Valid slugs: {', '.join(CATEGORY_SLUGS)}"
        )


def label(slug: str, lang: str = "fr") -> str:
    """Human-readable label for a category slug in the given language.

    Falls back to English when ``lang`` is unknown, and to the slug
    itself when the slug is unknown in the target language (which should
    only happen for newer slugs on older peers).
    """
    table = _LABELS_BY_LANG.get(lang) or _LABELS_EN
    return table.get(slug, slug)


def labeled_categories(lang: str = "fr") -> list[tuple[str, str]]:
    """Return ``[(slug, label), ...]`` in canonical display order."""
    return [(slug, label(slug, lang)) for slug in CATEGORY_SLUGS]
