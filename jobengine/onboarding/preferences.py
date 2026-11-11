"""Confirmed intake values and their compatibility with the existing matcher."""

from __future__ import annotations

from datetime import date

from jobengine.models import Constraint, IntakePreferences


def is_hard_constraint(constraint: Constraint) -> bool:
    """Apply current date policy to both new intake and older saved declarations."""
    return constraint.hard and constraint.field != "Graduation / start dates"


def validate_intake(intake: IntakePreferences) -> None:
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


def intake_constraints(intake: IntakePreferences) -> list[Constraint]:
    """No extra LLM parsing; blank optional preferences generate no restriction."""
    validate_intake(intake)
    constraints = []
    location = intake.location_constraint()
    if location:
        constraints.append(location)
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
    if intake.other_exclusions.strip():
        constraints.append(
            Constraint(
                id="C5", field="Other exclusions", value=intake.other_exclusions.strip(), hard=True
            )
        )
    return constraints


def resolved_intake_fields(intake: IntakePreferences) -> set[str]:
    # Optional blanks are an explicit lack of restriction, not unanswered facts.
    resolved = {"availability", "location"}
    if intake.graduation_status != "unknown":
        resolved.add("graduation")
    return resolved
