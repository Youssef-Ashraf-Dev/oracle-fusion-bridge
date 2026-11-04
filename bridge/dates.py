"""English day and month names that do not depend on the computer's language.

The standard date directives for a day name or a month name use the machine's locale. On a Windows PC set to Arabic that would put
Arabic names into a prompt written in English, or make a check look for the wrong word. These tables are always English.
"""
from __future__ import annotations

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def weekday(d) -> str:
    return WEEKDAYS[d.weekday()]


def month(d) -> str:
    return MONTHS[d.month - 1]
