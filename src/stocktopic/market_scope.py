"""Single source of truth for the live Shanghai/Shenzhen main-board scope."""

import re


def is_mainboard(code: str) -> bool:
    return bool(re.fullmatch(
        r"(?:(?:600|601|603|605)\d{3}\.SH|(?:000|001|002|003)\d{3}\.SZ)", str(code),
    ))


# SQL counterpart for existing stored rows; no history is deleted.
MAINBOARD_SQL = """(length(code)=9 AND (
    (substr(code,1,3) IN ('600','601','603','605') AND substr(code,7)='.SH')
    OR (substr(code,1,3) IN ('000','001','002','003') AND substr(code,7)='.SZ')
))"""
