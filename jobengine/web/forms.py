from __future__ import annotations

import re
import uuid

from django import forms
from django.forms import formset_factory

from jobengine.infrastructure.resumes import read_resume
from jobengine.models import (
    Document,
    GeneralInformation,
    IntakePreferences,
    OnboardingAnalysis,
    OnboardingSession,
)
from jobengine.onboarding.preferences import validate_intake


def lines(value):
    return [line.strip() for line in value.splitlines() if line.strip()]


class PreferencesForm(forms.Form):
    work_arrangements = forms.MultipleChoiceField(
        label="Work arrangement",
        required=False,
        choices=[("remote", "Remote"), ("hybrid", "Hybrid"), ("on_site", "On-site")],
        widget=forms.CheckboxSelectMultiple,
        help_text="Leave all unchecked if you're open to any arrangement.",
    )
    locations = forms.CharField(
        label="Cities or regions for on-site / hybrid work",
        required=False,
        max_length=500,
        help_text="Selected areas are limits. Remote work elsewhere must let you work from these areas. Leave blank to keep locations open.",
    )
    relocation = forms.ChoiceField(
        label="Open to relocating?",
        required=False,
        initial="no",
        choices=[("no", "No"), ("yes", "Yes, within my selected locations")],
        help_text="Selected locations remain limits. Remote work does not require relocation.",
    )

    def clean_relocation(self):
        return self.cleaned_data["relocation"] or "no"

    graduation_status = forms.ChoiceField(
        label="Education status",
        choices=[
            ("unknown", "Not sure / prefer to discuss"),
            ("graduated", "Graduated"),
            ("studying", "Currently studying"),
            ("not_applicable", "Not applicable"),
        ],
    )
    graduation_month = forms.IntegerField(
        label="Graduation month", min_value=1, max_value=12, required=False
    )
    graduation_year = forms.IntegerField(
        label="Graduation year", min_value=1950, max_value=2100, required=False
    )
    start_mode = forms.ChoiceField(
        label="When could you start?",
        choices=[
            ("any", "Flexible"),
            ("asap", "As soon as possible"),
            ("date", "From a specific date"),
        ],
    )
    start_date = forms.DateField(
        label="Earliest start date", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    other_exclusions = forms.CharField(
        label="Anything to avoid?",
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Optional. List firm exclusions, such as overnight shifts or frequent travel.",
    )

    @staticmethod
    def initial_for(intake):
        if not intake:
            return {}
        values = intake.model_dump(mode="json")
        values["locations"] = ", ".join(intake.locations)
        return values

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        if cleaned["graduation_status"] in {"graduated", "studying"} and (
            cleaned["graduation_month"] is None or cleaned["graduation_year"] is None
        ):
            raise forms.ValidationError(
                "Provide your graduation month and year, or choose Not sure yet."
            )
        payload = {
            name: cleaned[name] for name in IntakePreferences.model_fields if name in cleaned
        }
        payload["locations"] = [
            value.strip() for value in payload["locations"].split(",") if value.strip()
        ]
        try:
            self.intake = IntakePreferences.model_validate(payload)
            validate_intake(self.intake)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return cleaned


SCHOOLS = [
    "School University",
    "Northbridge University",
    "Westhaven College",
    "Cedar Valley Institute",
    "Lakewood State University",
]
AREAS = [
    "NYC Metropolitan Area",
    "NYC",
    "Long Island",
    "Hudson Valley",
    "Northern New Jersey",
    "Boston",
    "Greater Boston",
    "Philadelphia",
    "Greater Philadelphia",
    "Washington DC",
    "DC Metropolitan Area",
    "Baltimore",
    "Research Triangle",
    "Atlanta",
    "Greater Atlanta",
    "Miami",
    "South Florida",
    "Chicago",
    "Chicagoland",
    "Detroit",
    "Minneapolis-Saint Paul",
    "Dallas-Fort Worth",
    "Austin",
    "Houston",
    "Denver",
    "Colorado Front Range",
    "Phoenix",
    "Salt Lake City",
    "Las Vegas",
    "Los Angeles",
    "Orange County",
    "San Diego",
    "San Francisco",
    "San Francisco Bay Area",
    "Sacramento",
    "Portland",
    "Seattle",
    "Puget Sound",
]


class IntakeForm(forms.Form):
    graduation_month = forms.IntegerField(
        label="Graduation month", min_value=1, max_value=12, required=False
    )
    graduation_year = forms.IntegerField(
        label="Graduation year", min_value=1950, max_value=2100, required=False
    )
    full_name = forms.CharField(
        label="Full Name", max_length=150, widget=forms.TextInput(attrs={"autocomplete": "name"})
    )
    school = forms.ChoiceField(
        label="School",
        required=False,
        choices=[(name, name) for name in SCHOOLS],
        widget=forms.TextInput(
            attrs={
                "data-school-picker": "",
                "autocomplete": "off",
                "role": "combobox",
                "aria-autocomplete": "list",
                "aria-expanded": "false",
                "aria-controls": "school-options",
            }
        ),
        error_messages={"invalid_choice": "Choose a school from the list."},
    )
    degree = forms.ChoiceField(
        label="Degree",
        required=False,
        choices=[
            ("", "Select degree"),
            ("undergraduate", "Undergraduate"),
            ("graduate", "Graduate"),
            ("doctorate", "Doctorate"),
            ("associate", "Associate"),
            ("vocational", "Vocational / certificate"),
            ("other", "Other"),
            ("not_applicable", "Not applicable"),
        ],
    )
    program = forms.CharField(label="Program", max_length=200, required=False)
    graduation_status = forms.ChoiceField(
        label="Education status",
        choices=[
            ("", "Select education status"),
            ("graduated", "Graduated"),
            ("studying", "Currently studying"),
            ("not_applicable", "Not applicable"),
        ],
    )
    professional_years = forms.FloatField(
        label="Years of professional experience (including internships)",
        required=False,
        min_value=0,
        max_value=70,
        widget=forms.NumberInput(attrs={"step": "any", "inputmode": "decimal"}),
    )
    resume = forms.FileField(
        label="Your resume",
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": ".pdf,.docx,.txt,.md"}),
    )
    background = forms.CharField(
        label="Background summary",
        required=False,
        max_length=45000,
        widget=forms.Textarea(
            attrs={"rows": 4, "placeholder": "Your experience, skills and projects"}
        ),
    )
    interests = forms.CharField(
        label="Interests",
        required=False,
        max_length=400,
        widget=forms.TextInput(
            attrs={
                "placeholder": "e.g. Healthcare, Product design, Software Engineering",
                "data-interests": "",
                "aria-describedby": "id_interests_helptext",
            }
        ),
        help_text="Up to 8 interests, 1-3 words each. Separate them with commas.",
    )
    locations = forms.CharField(
        label="Cities or regions for on-site / hybrid work",
        required=False,
        max_length=500,
        widget=forms.TextInput(
            attrs={
                "data-area-picker": "",
                "autocomplete": "off",
                "role": "combobox",
                "aria-autocomplete": "list",
                "aria-expanded": "false",
                "aria-controls": "area-options",
                "aria-describedby": "id_locations_helptext",
            }
        ),
        help_text="Selected areas limit on-site and hybrid work. Remote jobs elsewhere must let you stay here. Leave blank to keep locations open.",
    )
    work_arrangements = forms.MultipleChoiceField(
        label="Work arrangement",
        required=False,
        choices=[("remote", "Remote"), ("hybrid", "Hybrid"), ("on_site", "On-site")],
        widget=forms.CheckboxSelectMultiple,
        help_text="Check all that apply.",
    )
    start_mode = forms.ChoiceField(
        label="When could you start?",
        choices=[
            ("any", "Flexible"),
            ("asap", "As soon as possible"),
            ("date", "From a specific date"),
        ],
    )
    start_date = forms.DateField(
        label="Earliest start date", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def clean_interests(self):
        interests = []
        for value in re.split(r"[,\n]", self.cleaned_data["interests"]):
            value = " ".join(value.split())
            if not value:
                continue
            if len(value.split()) > 3:
                raise forms.ValidationError("Keep each interest to one to three words.")
            if value.casefold() not in {item.casefold() for item in interests}:
                interests.append(value)
        if len(interests) > 8:
            raise forms.ValidationError("Choose up to 8 interests.")
        return ", ".join(interests)

    def clean_locations(self):
        selected = []
        options = {name.casefold(): name for name in AREAS}
        value = self.cleaned_data["locations"]
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            if item.casefold() not in options:
                raise forms.ValidationError("Choose cities or regions from the list.")
            area = options[item.casefold()]
            if area not in selected:
                selected.append(area)
        if len(selected) > 5:
            raise forms.ValidationError("Choose up to 5 areas.")
        self.selected_areas = selected
        return ", ".join(selected)

    def clean_resume(self):
        upload = self.cleaned_data["resume"]
        if not upload:
            return None
        if upload.size > 5_000_000:
            raise forms.ValidationError("Choose a resume smaller than 5 MB.")
        try:
            return Document(
                id="D1", label=upload.name, text=read_resume(upload.name, upload.read())
            )
        except Exception as exc:
            raise forms.ValidationError(
                "We couldn't read this file. Use a text-based PDF, DOCX, TXT, or Markdown file, or paste your background below."
            ) from exc

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        self.documents = [cleaned["resume"]] if cleaned["resume"] else []
        if cleaned["background"].strip():
            self.documents.append(
                Document(
                    id=f"D{len(self.documents) + 1}",
                    label="My background",
                    text=cleaned["background"].strip(),
                )
            )
        length = sum(len(document.text.strip()) for document in self.documents)
        if length < 30:
            raise forms.ValidationError(
                "Upload a resume or tell us a little more about your background (at least 30 characters)."
            )
        if length > 65000:
            raise forms.ValidationError(
                "Please keep your resume and background under 65,000 characters together."
            )
        degree_label = dict(self.fields["degree"].choices).get(cleaned["degree"], "")
        self.general_information = GeneralInformation(
            full_name=cleaned["full_name"],
            school=cleaned["school"],
            qualification=" / ".join(part for part in (degree_label, cleaned["program"]) if part),
        )
        details = [f"Full name: {cleaned['full_name']}"]
        for name in ("school", "degree", "program", "graduation_status"):
            value = cleaned[name]
            if value:
                field = self.fields[name]
                label = (
                    dict(field.choices).get(value, value) if hasattr(field, "choices") else value
                )
                details.append(f"{field.label}: {label}")
        self.documents.append(
            Document(
                id=f"D{len(self.documents) + 1}", label="Profile details", text="\n".join(details)
            )
        )
        self.intake = IntakePreferences(
            graduation_status=cleaned["graduation_status"],
            graduation_month=cleaned["graduation_month"],
            graduation_year=cleaned["graduation_year"],
            locations=self.selected_areas,
            work_arrangements=cleaned["work_arrangements"],
            start_mode=cleaned["start_mode"],
            start_date=cleaned["start_date"],
        )
        try:
            validate_intake(self.intake)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return cleaned

    def create_session(self):
        """Initialize saved onboarding from the validated website submission."""
        if not self.is_valid():
            raise ValueError("Validate the intake form before creating a session.")
        return OnboardingSession(
            id=uuid.uuid4().hex,
            documents=self.documents,
            role_hint="",
            interest_hint=self.cleaned_data["interests"],
            professional_years=self.cleaned_data["professional_years"],
            history=[],
            stage="intake",
            intake=self.intake,
            general_information=self.general_information,
            analysis=OnboardingAnalysis(
                summary="Your background is saved. Let's get to know you.",
                evidence=[],
                conflicts=[],
                missing_information=[],
                suggested_roles=[],
                suggested_interests="",
                preference_suggestions=[],
                next_question=None,
            ),
        )


class RevisionForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput)


class ProfileForm(RevisionForm):
    roles = forms.CharField(
        label="Roles to explore", max_length=1000, help_text="Separate roles with commas."
    )
    interests = forms.CharField(
        label="The work I want to do", max_length=5000, widget=forms.Textarea(attrs={"rows": 4})
    )
    professional_years = forms.FloatField(
        label="Years of professional experience", required=False, min_value=0, max_value=70
    )
    approved = forms.BooleanField(
        label="I've reviewed my experience, goals, and preferences. This profile represents me."
    )


class EvidenceForm(forms.Form):
    include = forms.BooleanField(label="Include this experience", required=False, initial=True)
    summary = forms.CharField(
        label="Description", max_length=3000, widget=forms.Textarea(attrs={"rows": 2})
    )


EvidenceFormSet = formset_factory(
    EvidenceForm, extra=0, max_num=100, validate_max=True, absolute_max=100
)


class BriefForm(RevisionForm):
    summary = forms.CharField(
        label="Your search objective", max_length=5000, widget=forms.Textarea(attrs={"rows": 3})
    )
    work_themes = forms.CharField(
        label="Work you'd like to do",
        max_length=3000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="One theme per line.",
    )
    search_queries = forms.CharField(
        label="Search queries",
        max_length=3000,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="One query per line, up to four.",
    )
    avoid = forms.CharField(
        label="What to avoid",
        required=False,
        max_length=3000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="One item per line.",
    )
    stretch_policy = forms.CharField(
        label="Where there's room to stretch",
        max_length=3000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    unresolved_questions = forms.CharField(
        label="What we still need to check",
        required=False,
        max_length=3000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="One question per line. Unknowns stay visible in the results.",
    )
    approved = forms.BooleanField(
        label="I approve this plan and the information that will guide my search."
    )


class SectorForm(forms.Form):
    name = forms.CharField(label="Industry / sector", max_length=200)
    relationship = forms.ChoiceField(
        label="Priority",
        choices=[
            ("preferred", "First choice"),
            ("related", "Related"),
            ("expanded", "Open to exploring"),
        ],
    )
    rationale = forms.CharField(
        label="Why explore this sector?", max_length=2000, widget=forms.Textarea(attrs={"rows": 2})
    )


class StrengthForm(forms.Form):
    statement = forms.CharField(
        label="Experience to build on", max_length=2000, widget=forms.Textarea(attrs={"rows": 2})
    )
    evidence_ids = forms.MultipleChoiceField(
        label="Experience that supports this",
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, evidence=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["evidence_ids"].choices = [(item.id, item.summary) for item in evidence]


SectorFormSet = formset_factory(
    SectorForm, extra=0, can_delete=True, max_num=20, validate_max=True, absolute_max=20
)
StrengthFormSet = formset_factory(
    StrengthForm, extra=0, can_delete=True, max_num=50, validate_max=True, absolute_max=50
)


class RestoreForm(forms.Form):
    backup = forms.FileField(
        label="Your JobEngine progress backup",
        widget=forms.ClearableFileInput(attrs={"accept": ".json"}),
        help_text="A progress export from this app or the previous Streamlit app. Up to 2 MB.",
    )

    def clean_backup(self):
        from jobengine.grounding import validate_evidence
        from jobengine.models import OnboardingSession, SavedOnboarding

        upload = self.cleaned_data["backup"]
        if upload.size > 2_000_000:
            raise forms.ValidationError("Choose a progress backup smaller than 2 MB.")
        payload = upload.read()
        try:
            try:
                record = SavedOnboarding.model_validate_json(payload)
            except ValueError:
                record = SavedOnboarding(session=OnboardingSession.model_validate_json(payload))
            session = record.session
            if (
                not session.documents
                or sum(len(document.text) for document in session.documents) > 65000
            ):
                raise ValueError("Invalid source length.")
            validate_evidence(session.analysis.evidence, session.documents)
            if session.intake:
                validate_intake(session.intake)
            if record.profile:
                validate_evidence(record.profile.evidence, session.documents)
                record.profile.documents = session.documents
                record.profile.intake = session.intake
                record.profile.confirmed = False
                record.profile.search_brief = None
            record.revision = 0
            record.brief_draft = None
            record.last_run_id = None
            session.pending_answer = session.pending_question = None
            session.stage = (
                "profile"
                if session.interview_finished
                else ("interview" if session.analysis.next_question else "intake")
            )
            return record
        except Exception as exc:
            raise forms.ValidationError(
                "We couldn't validate this progress backup. Choose a JobEngine progress JSON export with its original sources."
            ) from exc
