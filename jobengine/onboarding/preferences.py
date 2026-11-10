"""Confirmed intake values and their compatibility with the existing matcher."""

from __future__ import annotations

import math
from datetime import date

from jobengine.models import Constraint, IntakePreferences


def is_hard_constraint(constraint: Constraint) -> bool:
    """Apply current date policy to both new intake and older saved declarations."""
    return constraint.hard and constraint.field != "Graduation / start dates"


def validate_intake(intake: IntakePreferences) -> None:
    if not intake.authorization_country.strip() and any(
        value != "unknown"
        for value in (intake.authorized_to_work, intake.sponsorship_now, intake.sponsorship_future)
    ):
        raise ValueError("Choose the country for your work authorization.")
    if intake.graduation_status in {"graduated", "studying"} and (
        intake.graduation_month is not None or intake.graduation_year is not None
    ):
        if (
            intake.graduation_month is None
            or not 1 <= intake.graduation_month <= 12
            or intake.graduation_year is None
            or not 1950 <= intake.graduation_year <= 2100
        ):
            raise ValueError("Provide your graduation month and year, or choose Not sure yet.")
        if intake.graduation_status == "graduated" and (
            intake.graduation_year,
            intake.graduation_month,
        ) > (date.today().year, date.today().month):
            raise ValueError(
                "A completed graduation date cannot be in the future. Choose Currently studying if appropriate."
            )
    if intake.start_mode == "date" and intake.start_date is None:
        raise ValueError("Choose your earliest start date.")
    if intake.minimum_compensation is not None and (
        not math.isfinite(intake.minimum_compensation) or intake.minimum_compensation <= 0
    ):
        raise ValueError("Enter a positive compensation floor or leave it unrestricted.")


def intake_constraints(intake: IntakePreferences) -> list[Constraint]:
    """No extra LLM parsing; blank optional preferences generate no restriction."""
    validate_intake(intake)
    constraints = []
    location_parts = []
    if intake.locations:
        location_parts.append("Acceptable locations: " + ", ".join(intake.locations))
    if intake.work_arrangements:
        location_parts.append(
            "Work arrangements: " + ", ".join(x.replace("_", " ") for x in intake.work_arrangements)
        )
    if intake.remote_countries:
        location_parts.append("Remote work countries: " + ", ".join(intake.remote_countries))
    if intake.relocation != "any":
        location_parts.append("Willing to relocate: " + intake.relocation)
    if location_parts:
        constraints.append(
            Constraint(
                id="C1",
                field="Location / remote / relocation",
                value="; ".join(location_parts),
                hard=True,
            )
        )
    authorization = f"Country: {intake.authorization_country}; authorized to work: {intake.authorized_to_work}; sponsorship needed now: {intake.sponsorship_now}; sponsorship needed in future: {intake.sponsorship_future}."
    if intake.sponsorship_now == "no" and intake.sponsorship_future == "no":
        authorization += " Applicant does not require employer sponsorship; whether an employer offers sponsorship is not an applicant requirement."
    if intake.authorization_country.strip():
        constraints.append(
            Constraint(
                id="C2", field="Work authorization / sponsorship", value=authorization, hard=True
            )
        )
    graduation = {
        "graduated": "Graduated",
        "studying": "Expected graduation",
        "not_applicable": "No degree / graduation date not applicable",
        "unknown": "Graduation status/date unknown",
    }[intake.graduation_status]
    if intake.graduation_status in {"graduated", "studying"} and intake.graduation_year is not None:
        graduation += f": {intake.graduation_year:04d}-{intake.graduation_month:02d}"
    availability = {
        "any": "No start-date restriction",
        "asap": "Available to start immediately",
        "date": f"Earliest start: {intake.start_date}",
    }[intake.start_mode]
    constraints.append(
        Constraint(
            id="C3",
            field="Graduation / start dates",
            value=graduation + "; " + availability,
            hard=False,
        )
    )
    if intake.minimum_compensation is not None:
        constraints.append(
            Constraint(
                id="C4",
                field="Compensation",
                value=f"Minimum annual {intake.compensation_basis} compensation: {intake.currency} {intake.minimum_compensation:,.0f}",
                hard=True,
            )
        )
    if intake.other_exclusions.strip():
        constraints.append(
            Constraint(
                id="C5", field="Other exclusions", value=intake.other_exclusions.strip(), hard=True
            )
        )
    return constraints


def resolved_intake_fields(intake: IntakePreferences) -> set[str]:
    # Optional blanks are an explicit lack of restriction, not unanswered facts.
    resolved = {"availability", "location", "compensation"}
    if intake.graduation_status != "unknown":
        resolved.add("graduation")
    if intake.authorized_to_work != "unknown":
        resolved.add("authorization")
    if intake.sponsorship_now != "unknown" and intake.sponsorship_future != "unknown":
        resolved.add("sponsorship")
    return resolved
