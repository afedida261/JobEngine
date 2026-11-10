from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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


class IntakePreferences(Model):
    authorization_country: str
    authorized_to_work: Literal["yes", "no", "unknown"]
    sponsorship_now: Literal["yes", "no", "unknown"]
    sponsorship_future: Literal["yes", "no", "unknown"]
    graduation_status: Literal["graduated", "studying", "not_applicable", "unknown"]
    graduation_month: int | None = None
    graduation_year: int | None = None
    start_mode: Literal["any", "asap", "date"] = "any"
    start_date: date | None = None
    work_arrangements: list[Literal["on_site", "hybrid", "remote"]] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_countries: list[str] = Field(default_factory=list)
    relocation: Literal["any", "yes", "no"] = "any"
    minimum_compensation: float | None = None
    currency: str = "USD"
    compensation_basis: Literal["base", "total"] = "base"
    other_exclusions: str = ""


class BriefUncertainty(Model):
    subject: Literal["applicant", "employer", "capability"]
    field: Literal[
        "graduation",
        "availability",
        "authorization",
        "sponsorship",
        "location",
        "compensation",
        "other",
    ]
    question: str


class BriefProposal(Model):
    plan: SearchBriefDraft
    uncertainties: list[BriefUncertainty]


class Profile(Model):
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
        "Work authorization / sponsorship",
        "Graduation / start dates",
        "Compensation",
        "Other exclusions",
    ]
    value: str
    hard: bool
    source_id: str
    quote: str


class OnboardingAnalysis(Model):
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
    field: Literal[
        "location", "work_country", "compensation", "authorization", "graduation", "start_date"
    ]
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
