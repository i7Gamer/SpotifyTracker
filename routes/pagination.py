# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Small shared pagination helpers for route modules."""

from flask import request


# Past this, ?page= is not a page anyone can be on - it is a typo or a crafted
# URL, and treating it as either costs nothing. Well under CPython's 4300-digit
# int/str conversion limit, which is what makes the check worth having.
_MAX_PAGE_DIGITS = 9


def positivePageArg():
    """?page= as a string when it names a real page, "" otherwise.

    The two-phase shells use it to decide whether to carry the page into the
    URL their placeholder loads from: junk gets left out rather than echoed,
    same reasoning as the validated interval beside them. For those it is not
    validation, because the list request clamps the page against the row count.

    topListMovement is the exception and reads ?page= itself - it runs no count
    query, so nothing downstream would catch an absurd page for it.

    The length check comes before int(): CPython refuses to convert a string of
    more than 4300 digits, so `isdigit() and int(raw)` was an unhandled
    ValueError - a 500 on four shells, from a URL anyone can type.

    isdecimal(), not isdigit(): isdigit() is also true for characters like
    '²' (superscript two) or '①' (circled one) that int() refuses - the same
    unhandled ValueError, just from a shorter string. isdecimal() is exactly
    what int() accepts.
    """
    raw = request.args.get("page", "")
    if not raw.isdecimal() or len(raw) > _MAX_PAGE_DIGITS:
        return ""
    return raw if int(raw) > 0 else ""
