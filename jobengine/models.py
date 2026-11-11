from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Document(Model):
    id: str
    label: str
    text: str


class Evidence(Model):
    id: str
    kind: Literal["education", "project", "professional_experience", "skill", "other"]
    summary: str
    source_id: str
    quote: str


class Question(Model):
    question: str
    purpose: str
    topic: str | None = None
    project_key: str | None = None


class SearchStrength(Model):
    statement: str
    evidence_ids: list[str]


class SectorTarget(Model):
    name: str
    relationship: Literal["preferred", "related", "expanded"]
    rationale: str


class SearchBriefDraft(Model):
    summary: str
    target_roles: list[str]
    work_themes: list[str]
    strengths: list[SearchStrength]
    search_queries: list[str]
    avoid: list[str]
    stretch_policy: str
    unresolved_questions: list[str]
    sectors: list[SectorTarget] = Field(default_factory=list)


class SearchBrief(SearchBriefDraft):
    profile_fingerprint: str
    confirmed: bool = False
    confirmation_fingerprint: str | None = None


class Constraint(Model):
    id: str
    field: str
    value: str
    hard: bool


# Read compatibility only: retired intake keys never enter current schemas or exports.
RETIRED_INTAKE_FIELDS = frozenset(
    {
        "authorization_country",
        "authorized_to_work",
        "sponsorship_now",
        "sponsorship_future",
        "remote_countries",
        "minimum_compensation",
        "currency",
        "compensation_basis",
    }
)
RETIRED_CONSTRAINT_FIELDS = frozenset({"Work authorization / sponsorship", "Compensation"})


def current_constraints(values):
    """Discard retired constraints in old exports, retaining supported preferences."""
    result = []
    for item in values:
        value = item.model_dump() if isinstance(item, BaseModel) else dict(item)
        if value.get("field") in RETIRED_CONSTRAINT_FIELDS:
            continue
        if value.get("field") == "Location / remote / relocation":
            value["value"] = "; ".join(
                part.strip()
                for part in value["value"].split(";")
                if not part.strip().startswith("Remote work countries:")
            )
            if not value["value"]:
                continue
        result.append(value)
    return result


class IntakePreferences(Model):
    @model_validator(mode="before")
    @classmethod
    def load_previous_intake(cls, values):
        if isinstance(values, dict):
            values = {
                key: value for key, value in values.items() if key not in RETIRED_INTAKE_FIELDS
            }
            if values.get("relocation") == "any":
                values["relocation"] = "no"
        return values

    graduation_status: Literal["graduated", "studying", "not_applicable", "unknown"]
    graduation_month: int | None = None
    graduation_year: int | None = None
    start_mode: Literal["any", "asap", "date"] = "any"
    start_date: date | None = None
    work_arrangements: list[Literal["on_site", "hybrid", "remote"]] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    relocation: Literal["yes", "no"] = "no"
    other_exclusions: str = ""

    def location_constraint(self) -> Constraint | None:
        """Separate required workplace locations from the employer's remote location."""
        parts = []
        if self.locations:
            parts.append("On-site and hybrid work must be in: " + ", ".join(self.locations))
            if not self.work_arrangements or "remote" in self.work_arrangements:
                parts.append(
                    "Remote work for employers elsewhere in the United States is allowed only if "
                    "the applicant can work from the selected locations without relocating"
                )
        if self.work_arrangements:
            parts.append(
                "Work arrangements: "
                + ", ".join(x.replace("_", " ") for x in self.work_arrangements)
            )
        if parts:
            parts.append("Willing to relocate: " + self.relocation)
            parts.append("Relocation does not expand the selected locations")
            return Constraint(
                id="C1", field="Location / remote / relocation", value="; ".join(parts), hard=True
            )
        return None


class BriefUncertainty(Model):
    subject: Literal["applicant", "employer", "capability"]
    field: Literal[
        "graduation",
        "availability",
        "location",
        "other",
    ]
    question: str


class BriefProposal(Model):
    @model_validator(mode="before")
    @classmethod
    def load_previous_uncertainties(cls, values):
        if isinstance(values, dict) and "uncertainties" in values:
            return values | {
                "uncertainties": [
                    item
                    for item in values.get("uncertainties", [])
                    if (item.field if isinstance(item, BaseModel) else item.get("field"))
                    not in {"authorization", "sponsorship", "compensation"}
                ]
            }
        return values

    plan: SearchBriefDraft
    uncertainties: list[BriefUncertainty]


class Profile(Model):
    @model_validator(mode="before")
    @classmethod
    def load_previous_preferences(cls, values):
        if not isinstance(values, dict):
            return values
        values = dict(values)
        if "constraints" not in values:
            return values
        previous = values.get("constraints", [])
        current = current_constraints(previous)
        intake = values.get("intake")
        retired = isinstance(intake, dict) and bool(RETIRED_INTAKE_FIELDS.intersection(intake))
        changed_relocation = isinstance(intake, dict) and intake.get("relocation") in {None, "any"}
        if intake:
            normalized_intake = IntakePreferences.model_validate(intake)
            location = normalized_intake.location_constraint()
            current = [
                constraint
                for constraint in current
                if constraint.get("field") != "Location / remote / relocation"
            ]
            if location and not any(c.get("id") == location.id for c in current):
                current.insert(0, location.model_dump())
        if (
            current != [c.model_dump() if isinstance(c, BaseModel) else c for c in previous]
            or retired
            or changed_relocation
        ):
            values["confirmed"] = False
            values["search_brief"] = None
        values["constraints"] = current
        return values

    documents: list[Document]
    evidence: list[Evidence]
    target_roles: list[str]
    interests: str
    constraints: list[Constraint]
    professional_years: float | None
    confirmed: bool = False
    search_brief: SearchBrief | None = None
    intake: IntakePreferences | None = None


class InterviewExchange(Model):
    question: str
    purpose: str
    answer: str | None
    source_id: str | None
    topic: str | None = None
    project_key: str | None = None
    created_at: str | None = None


class AnswerCheck(Model):
    disposition: Literal["relevant", "off_topic", "move_on"]


class InterviewAttempt(Model):
    question: str
    text: str
    disposition: Literal["off_topic", "move_on"]
    created_at: str = Field(default_factory=utc_now)


class PreferenceSuggestion(Model):
    field: Literal[
        "Location / remote / relocation",
        "Graduation / start dates",
        "Other exclusions",
    ]
    value: str
    hard: bool
    source_id: str
    quote: str


class OnboardingAnalysis(Model):
    @model_validator(mode="before")
    @classmethod
    def load_previous_suggestions(cls, values):
        if isinstance(values, dict) and "preference_suggestions" in values:
            return values | {
                "preference_suggestions": current_constraints(
                    values.get("preference_suggestions", [])
                )
            }
        return values

    summary: str
    evidence: list[Evidence]
    conflicts: list[str]
    missing_information: list[str]
    suggested_roles: list[str]
    suggested_interests: str
    preference_suggestions: list[PreferenceSuggestion]
    next_question: Question | None
    superseded_evidence_ids: list[str] = Field(default_factory=list)


class GeneralInformation(Model):
    full_name: str
    current_location: str = ""
    school: str = ""
    qualification: str = ""


class OnboardingSession(Model):
    id: str
    documents: list[Document]
    role_hint: str
    interest_hint: str
    professional_years: float | None
    history: list[InterviewExchange]
    analysis: OnboardingAnalysis
    interview_finished: bool = False
    stage: Literal["intake", "interview", "profile", "brief", "ready"] = "interview"
    intake: IntakePreferences | None = None
    general_information: GeneralInformation | None = None
    attempts: list[InterviewAttempt] = Field(default_factory=list)
    notice: str = ""
    stop_project_questions: bool = False
    pending_answer: str | None = None
    pending_question: str | None = None
    interview_mode: Literal["adaptive", "guided"] = "adaptive"


class SavedOnboarding(Model):
    @model_validator(mode="before")
    @classmethod
    def review_migrated_preferences(cls, values):
        if not isinstance(values, dict) or not isinstance(values.get("profile"), dict):
            return values
        previous = values["profile"]
        profile = Profile.model_validate(previous)
        if previous.get("confirmed") and not profile.confirmed:
            values = dict(values)
            values["profile"] = profile
            values["brief_draft"] = None
            values["last_run_id"] = None
            session = values["session"]
            values["session"] = (
                session.model_copy(update={"stage": "profile"})
                if isinstance(session, OnboardingSession)
                else session | {"stage": "profile"}
            )
        return values

    session: OnboardingSession
    profile: Profile | None = None
    brief_draft: SearchBriefDraft | None = None
    last_run_id: str | None = None
    revision: int = 0
    updated_at: str = Field(default_factory=utc_now)


class DiscoveryItem(Model):
    title: str
    company: str
    url: str
    sector: str | None = None
    company_source_url: str | None = None


class DiscoveryOutput(Model):
    jobs: list[DiscoveryItem]


class SectorProposal(Model):
    sectors: list[SectorTarget]


class CompanyCandidate(Model):
    name: str
    official_url: str
    careers_url: str | None
    sector_rationale: str
    sector: str = ""
    early_career_evidence_url: str | None = None


class CompanyOutput(Model):
    companies: list[CompanyCandidate]


class SearchStep(Model):
    stage: Literal["companies", "jobs", "evaluation"]
    sector: str
    company: str | None = None
    queries: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    candidates: int = 0
    outcome: str = ""


class SearchRecord(Model):
    query: str
    retrieved_at: str
    summary: str
    sources: list[dict[str, str]]
    jobs: list[DiscoveryItem]
    warnings: list[str]
    strategy: str = "web"
    sectors: list[SectorTarget] = Field(default_factory=list)
    companies: list[CompanyCandidate] = Field(default_factory=list)
    steps: list[SearchStep] = Field(default_factory=list)
    stop_reason: str = ""


class EmployerTerm(Model):
    field: Literal["location", "work_country", "graduation", "start_date"]
    value: str
    quote: str


class JobPage(Model):
    requested_url: str
    url: str
    retrieved_at: str
    text: str
    content_hash: str
    extraction_method: str
    status: Literal["retrieved", "failed"]
    error: str | None = None


class Requirement(Model):
    id: str
    text: str
    quote: str
    importance: Literal["required", "preferred", "responsibility"]
    minimum_professional_years: float | None
    kind: Literal["qualification", "graduation_cohort"] = "qualification"


class JobFacts(Model):
    @model_validator(mode="before")
    @classmethod
    def load_previous_terms(cls, values):
        if isinstance(values, dict):
            return values | {
                "terms": [
                    term
                    for term in values.get("terms", [])
                    if (term.field if isinstance(term, EmployerTerm) else term.get("field"))
                    not in {"authorization", "compensation"}
                ]
            }
        return values

    is_job_posting: bool
    title: str
    company: str
    location: str | None
    posted_at: str | None
    valid_through: str | None
    availability: Literal["open", "closed", "unknown"]
    availability_quote: str | None
    requirements: list[Requirement]
    extraction_notes: list[str]
    terms: list[EmployerTerm] = Field(default_factory=list)


class RequirementAssessment(Model):
    requirement_id: str
    status: Literal["meets", "gap", "unknown"]
    evidence_ids: list[str]
    supporting_constraint_ids: list[str] = Field(default_factory=list)
    explanation: str


class ConstraintAssessment(Model):
    constraint_id: str
    status: Literal["meets", "conflict", "unknown"]
    job_quote: str | None
    explanation: str


class Differentiator(Model):
    requirement_id: str
    evidence_ids: list[str]
    explanation: str


class InterestAlignment(Model):
    level: Literal["high", "medium", "low", "unknown"]
    job_quote: str | None
    explanation: str


class FitJudgment(Model):
    requirements: list[RequirementAssessment]
    constraints: list[ConstraintAssessment]
    differentiators: list[Differentiator]
    interest_alignment: InterestAlignment
    uncertainties: list[str]


class RankedJob(Model):
    discovery: DiscoveryItem
    page: JobPage
    facts: JobFacts | None
    judgment: FitJudgment | None
    category: Literal["strong", "weaker", "needs_review", "excluded", "unverified"]
    score: float
    reasons: list[str]
    validation_issues: list[str]
    keyword_score: float


class Run(Model):
    id: str
    created_at: str = Field(default_factory=utc_now)
    model: str
    profile: Profile
    search: SearchRecord
    jobs: list[RankedJob]
    inspected_jobs: list[RankedJob] = Field(default_factory=list)
    purpose: Literal["user", "validation"] = "user"

    def review_items(self) -> list[DiscoveryItem]:
        visible = {job.discovery.url for job in self.jobs}
        return [item for item in self.search.jobs if item.url in visible][:10]
