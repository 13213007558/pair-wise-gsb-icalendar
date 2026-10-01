"""This package contains all functionality for timezones."""

from .tzid import is_utc, tzid_from_dt, tzid_from_tzinfo, tzids_from_tzinfo
from .tzp import TZP
from .vtimezone import (
    FoldPolicy,
    GapPolicy,
    LocalTimeCandidate,
    LocalTimeDiagnosis,
    LocalTimeKind,
    Transition,
    VTIMEZONEInfo,
    diagnose_local,
    localize_naive,
)

tzp = TZP()


def use_pytz():
    """Use pytz as the implementation that looks up and creates timezones."""
    tzp.use_pytz()


def use_zoneinfo():
    """Use zoneinfo as the implementation that looks up and creates timezones."""
    tzp.use_zoneinfo()


__all__ = [
    "TZP",
    "FoldPolicy",
    "GapPolicy",
    "LocalTimeCandidate",
    "LocalTimeDiagnosis",
    "LocalTimeKind",
    "Transition",
    "VTIMEZONEInfo",
    "diagnose_local",
    "is_utc",
    "localize_naive",
    "tzid_from_dt",
    "tzid_from_tzinfo",
    "tzids_from_tzinfo",
    "tzp",
    "use_pytz",
    "use_zoneinfo",
]
