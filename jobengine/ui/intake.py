"""Explicit preferences controls shared by first intake and later correction."""

from __future__ import annotations

from calendar import month_name

import streamlit as st

from jobengine.models import IntakePreferences
from jobengine.onboarding.preferences import intake_constraints, validate_intake

GRADUATION = {
    "Graduated": "graduated",
    "Currently studying": "studying",
    "No degree / not applicable": "not_applicable",
    "Not sure yet": "unknown",
}
START = {"Any / no restriction": "any", "As soon as possible": "asap", "On or after a date": "date"}
ARRANGEMENTS = {"On-site": "on_site", "Hybrid": "hybrid", "Remote": "remote"}
RELOCATION = {
    "Stay within selected locations": "no",
    "Can relocate within selected locations": "yes",
}


def _selection(options, value):
    return next((i for i, key in enumerate(options) if options[key] == value), None)


def intake_inputs(prefix: str, previous: IntakePreferences | None = None) -> dict:
    st.markdown("**Graduation · required**")
    status_col, month_col, year_col = st.columns([2, 1, 1])
    status = status_col.selectbox(
        "Graduation status",
        list(GRADUATION),
        index=_selection(GRADUATION, previous.graduation_status) if previous else None,
        key=prefix + "graduation_status",
    )
    month = month_col.selectbox(
        "Graduation month",
        list(range(1, 13)),
        index=previous.graduation_month - 1 if previous and previous.graduation_month else None,
        format_func=lambda n: month_name[n],
        key=prefix + "graduation_month",
    )
    year = year_col.number_input(
        "Graduation year",
        min_value=1950,
        max_value=2100,
        value=previous.graduation_year if previous else None,
        step=1,
        placeholder="YYYY",
        key=prefix + "graduation_year",
    )
    st.caption(
        "Month/year are needed only for Graduated or Currently studying. Not sure is an explicit unknown."
    )
    with st.expander("Optional start and location preferences", expanded=previous is not None):
        st.caption(
            "Selected locations limit on-site and hybrid work. Remote work elsewhere must let you stay in those locations."
        )
        left, right = st.columns(2)
        start = left.selectbox(
            "When can you start?",
            list(START),
            index=_selection(START, previous.start_mode) if previous else 0,
            key=prefix + "start",
        )
        start_date = right.date_input(
            "Earliest start date (if choosing a date)",
            value=previous.start_date if previous else None,
            key=prefix + "start_date",
        )
        arrangements = st.multiselect(
            "Acceptable work arrangements",
            list(ARRANGEMENTS),
            default=[
                k for k, v in ARRANGEMENTS.items() if previous and v in previous.work_arrangements
            ],
            key=prefix + "arrangements",
        )
        locations = st.multiselect(
            "Acceptable cities / regions",
            [
                "New York City",
                "Long Island",
                "San Francisco Bay Area",
                "Boston",
                "Washington, DC",
            ],
            default=previous.locations if previous else [],
            accept_new_options=True,
            placeholder="No location restriction; add a city or region if needed",
            key=prefix + "locations",
        )
        relocation = st.selectbox(
            "Relocation",
            list(RELOCATION),
            index=_selection(RELOCATION, previous.relocation) if previous else 0,
            key=prefix + "relocation",
        )
        exclusions = st.text_input(
            "Other exclusions (optional)",
            value=previous.other_exclusions if previous else "",
            placeholder="e.g. contract-only work or industries you want to avoid",
            key=prefix + "exclusions",
        )
    return dict(
        status=status,
        month=month,
        year=year,
        start=start,
        start_date=start_date,
        arrangements=arrangements,
        locations=locations,
        relocation=relocation,
        exclusions=exclusions,
    )


def parse_intake(values: dict) -> IntakePreferences:
    if values["status"] is None or values["status"] == "":
        raise ValueError(
            "Complete the graduation section. You can choose Not sure where appropriate."
        )
    status = GRADUATION[values["status"]]
    intake = IntakePreferences(
        graduation_status=status,
        graduation_month=values["month"] if status in {"graduated", "studying"} else None,
        graduation_year=values["year"] if status in {"graduated", "studying"} else None,
        start_mode=START[values["start"]],
        start_date=values["start_date"] if START[values["start"]] == "date" else None,
        work_arrangements=[ARRANGEMENTS[a] for a in values["arrangements"]],
        locations=values["locations"],
        relocation=RELOCATION[values["relocation"]],
        other_exclusions=values["exclusions"].strip(),
    )
    validate_intake(intake)
    return intake


def show_intake(intake: IntakePreferences):
    for constraint in intake_constraints(intake):
        st.write(f"**{constraint.field}:** {constraint.value}")
