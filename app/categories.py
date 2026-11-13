"""Plain names for the supplier's numeric category codes (config/categories.yaml)."""

from __future__ import annotations

from functools import lru_cache

import yaml

from app import settings


@lru_cache(maxsize=1)
def _by_code() -> dict[str, str]:
    groups = yaml.safe_load(settings.CATEGORIES_FILE.read_text(encoding="utf-8")) or {}
    return {str(code): name for name, codes in groups.items() for code in codes}


def name_of(code: str | None) -> str | None:
    """The plain name of a category code, or None when the code is not mapped."""
    return _by_code().get(str(code)) if code else None


def all_names() -> list[str]:
    return sorted(set(_by_code().values()))
