"""Explicit preferences controls shared by first intake and later correction."""

from __future__ import annotations

from calendar import month_name

import streamlit as st

from jobengine.models import IntakePreferences
from jobengine.onboarding.preferences import intake_constraints, validate_intake

COUNTRIES = [
    "United States",
    "Israel",
    "Canada",
    "United Kingdom",
    "Germany",
    "France",
    "Netherlands",
    "Australia",
    "India",
    "Other country",
]
GRADUATION = {
    "Graduated": "graduated",
    "Currently studying": "studying",
    "No degree / not applicable": "not_applicable",
    "Not sure yet": "unknown",
}
YES_NO = {"Yes": "yes", "No": "no", "Not sure": "unknown"}
START = {"Any / no restriction": "any", "As soon as possible": "asap", "On or after a date": "date"}
ARRANGEMENTS = {"On-site": "on_site", "Hybrid": "hybrid", "Remote": "remote"}
RELOCATION = {"No preference": "any", "Open to relocation": "yes", "Cannot relocate": "no"}


def _selection(options, value):
    return next((i for i, key in enumerate(options) if options[key] == value), None)


def intake_inputs(prefix: str, previous: IntakePreferences | None = None) -> dict:
    st.markdown("**Work authorization · required**")
    left, right = st.columns(2)
    country_value = previous.authorization_country if previous else None
    country_index = (
        COUNTRIES.index(country_value)
        if country_value in COUNTRIES
        else len(COUNTRIES) - 1
        if country_value
        else None
    )
    country = left.selectbox(
        "Country where you want to work", COUNTRIES, index=country_index, key=prefix + "country"
    )
    other_country = right.text_input(
        "Other country name (if needed)",
        value=country_value if country_value and country_value not in COUNTRIES else "",
        key=prefix + "other_country",
    )
    authorized = st.selectbox(
        "Are you authorized to work in that country?",
        list(YES_NO),
        index=_selection(YES_NO, previous.authorized_to_work) if previous else None,
        key=prefix + "authorized",
    )
    left, right = st.columns(2)
    now = left.selectbox(
        "Need sponsorship now?",
        list(YES_NO),
        index=_selection(YES_NO, previous.sponsorship_now) if previous else None,
        key=prefix + "sponsorship_now",
    )
    future = right.selectbox(
        "Need sponsorship in the future?",
        list(YES_NO),
        index=_selection(YES_NO, previous.sponsorship_future) if previous else None,
        key=prefix + "sponsorship_future",
    )
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
    with st.expander(
        "Optional start, location, and pay preferences", expanded=previous is not None
    ):
        st.caption(
            "Leave optional preferences blank for no restriction. Explicit choices such as Cannot relocate will be honored."
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
                "Tel Aviv",
                "London",
            ],
            default=previous.locations if previous else [],
            accept_new_options=True,
            placeholder="No location restriction; add a city or region if needed",
            key=prefix + "locations",
        )
        remote = st.multiselect(
            "Remote work countries",
            COUNTRIES[:-1],
            default=previous.remote_countries if previous else [],
            accept_new_options=True,
            placeholder="No extra remote-country restriction",
            key=prefix + "remote_countries",
        )
        relocation = st.selectbox(
            "Relocation",
            list(RELOCATION),
            index=_selection(RELOCATION, previous.relocation) if previous else 0,
            key=prefix + "relocation",
        )
        amount_col, currency_col, basis_col = st.columns([2, 1, 2])
        amount = amount_col.number_input(
            "Minimum annual compensation",
            min_value=0.0,
            value=float(previous.minimum_compensation or 0) if previous else 0.0,
            step=5000.0,
            help="Zero means no minimum.",
            key=prefix + "compensation",
        )
        currencies = ["USD", "ILS", "EUR", "GBP", "CAD", "AUD", "INR"]
        currency = currency_col.selectbox(
            "Currency",
            currencies,
            index=currencies.index(previous.currency)
            if previous and previous.currency in currencies
            else 0,
            key=prefix + "currency",
        )
        basis = basis_col.selectbox(
            "Compensation basis",
            ["Base salary", "Total compensation"],
            index=1 if previous and previous.compensation_basis == "total" else 0,
            key=prefix + "compensation_basis",
        )
        exclusions = st.text_input(
            "Other exclusions (optional)",
            value=previous.other_exclusions if previous else "",
            placeholder="e.g. contract-only work or industries you want to avoid",
            key=prefix + "exclusions",
        )
    return dict(
        country=other_country.strip() if country == "Other country" else country,
        authorized=authorized,
        now=now,
        future=future,
        status=status,
        month=month,
        year=year,
        start=start,
        start_date=start_date,
        arrangements=arrangements,
        locations=locations,
        remote=remote,
        relocation=relocation,
        amount=amount,
        currency=currency,
        basis=basis,
        exclusions=exclusions,
    )


def parse_intake(values: dict) -> IntakePreferences:
    if any(
        values[key] is None or values[key] == ""
        for key in ("country", "authorized", "now", "future", "status")
    ):
        raise ValueError(
            "Complete the work authorization and graduation sections. You can choose Not sure where appropriate."
        )
    status = GRADUATION[values["status"]]
    intake = IntakePreferences(
        authorization_country=values["country"],
        authorized_to_work=YES_NO[values["authorized"]],
        sponsorship_now=YES_NO[values["now"]],
        sponsorship_future=YES_NO[values["future"]],
        graduation_status=status,
        graduation_month=values["month"] if status in {"graduated", "studying"} else None,
        graduation_year=values["year"] if status in {"graduated", "studying"} else None,
        start_mode=START[values["start"]],
        start_date=values["start_date"] if START[values["start"]] == "date" else None,
        work_arrangements=[ARRANGEMENTS[a] for a in values["arrangements"]],
        locations=values["locations"],
        remote_countries=values["remote"],
        relocation=RELOCATION[values["relocation"]],
        minimum_compensation=values["amount"] or None,
        currency=values["currency"],
        compensation_basis="base" if values["basis"] == "Base salary" else "total",
        other_exclusions=values["exclusions"].strip(),
    )
    validate_intake(intake)
    return intake


def show_intake(intake: IntakePreferences):
    for constraint in intake_constraints(intake):
        st.write(f"**{constraint.field}:** {constraint.value}")
