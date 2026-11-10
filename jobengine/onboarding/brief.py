"""Search-plan proposal, validation, and approval fingerprints."""

from __future__ import annotations

import hashlib
import json

from jobengine.grounding import validate_evidence
from jobengine.infrastructure.llm import ModelClient
from jobengine.models import BriefProposal, Profile, SearchBrief, SearchBriefDraft, SectorProposal
from jobengine.onboarding.preferences import is_hard_constraint, resolved_intake_fields
from jobengine.prompts import PROMPTS


def profile_fingerprint(profile: Profile) -> str:
    payload = profile.model_dump(mode="json", exclude={"confirmed", "search_brief"})
    if profile.intake is None:
        payload.pop("intake", None)  # Preserve approvals on unmodified legacy exports.
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def validate_search_brief(profile: Profile, brief: SearchBriefDraft) -> None:
    validate_evidence(profile.evidence, profile.documents)
    if (
        not brief.summary.strip()
        or not brief.target_roles
        or not brief.search_queries
        or not brief.work_themes
    ):
        raise ValueError(
            "Include a search summary, target roles, work themes, and at least one concrete query."
        )
    if any(
        not value.strip() for value in brief.target_roles + brief.search_queries + brief.work_themes
    ):
        raise ValueError("Remove blank roles, themes, or queries.")
    if not 1 <= len(brief.search_queries) <= 4:
        raise ValueError("Use between one and four focused search queries.")
    if {r.casefold().strip() for r in brief.target_roles} != {
        r.casefold().strip() for r in profile.target_roles
    }:
        raise ValueError(
            "The brief's roles must match the reviewed profile. Edit roles in the profile first."
        )
    evidence_ids = {e.id for e in profile.evidence}
    if not brief.strengths:
        raise ValueError(
            "Include at least one evidence-backed strength or area of experience to guide discovery."
        )
    for strength in brief.strengths:
        if (
            not strength.statement.strip()
            or not strength.evidence_ids
            or any(e not in evidence_ids for e in strength.evidence_ids)
        ):
            raise ValueError(
                "Every search strength must reference existing candidate evidence IDs."
            )
    if not brief.stretch_policy.strip():
        raise ValueError(
            "Describe which stretches should be considered and which requirements remain firm."
        )
    if any(not s.name.strip() or not s.rationale.strip() for s in brief.sectors):
        raise ValueError("Give each sector a name and a reason.")
    if len({s.name.strip().casefold() for s in brief.sectors}) != len(brief.sectors):
        raise ValueError("Remove duplicate sectors.")


def propose_search_brief(client: ModelClient, profile: Profile) -> SearchBriefDraft:
    if not profile.confirmed:
        raise ValueError("Review and confirm the candidate profile first.")
    instructions = PROMPTS["search_brief"]
    data = {
        "evidence": [e.model_dump() for e in profile.evidence],
        "target_roles": profile.target_roles,
        "interests": profile.interests,
        "professional_years": profile.professional_years,
        "constraints": [
            c.model_dump() | {"hard": is_hard_constraint(c)} for c in profile.constraints
        ],
        "confirmed_intake": profile.intake.model_dump(mode="json") if profile.intake else None,
    }
    if profile.intake:
        proposal = client.structured(
            "onboarding_search_brief",
            instructions + PROMPTS["brief_uncertainties"],
            data,
            BriefProposal,
        )
        result = proposal.plan
        resolved = resolved_intake_fields(profile.intake)
        result.unresolved_questions = [
            u.question
            for u in proposal.uncertainties
            if u.subject != "applicant" or u.field not in resolved
        ]
    else:
        result = client.structured("onboarding_search_brief", instructions, data, SearchBriefDraft)
    result.sectors = _order_sector_proposal(result.sectors)
    validate_search_brief(profile, result)
    return result


def _confirmation_fingerprint(profile: Profile, brief: SearchBriefDraft) -> str:
    content = {
        "profile": profile_fingerprint(profile),
        "brief": brief.model_dump(include=set(SearchBriefDraft.model_fields)),
    }
    if not brief.sectors:
        content["brief"].pop("sectors", None)  # Unmodified older exports remain readable.
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _order_sector_proposal(sectors):
    return sorted(
        sectors, key=lambda s: {"preferred": 0, "related": 1, "expanded": 2}[s.relationship]
    )


def propose_sectors(client: ModelClient, profile: Profile) -> SectorProposal:
    result = client.structured(
        "sector_plan",
        PROMPTS["sector_plan"],
        {
            "target_roles": profile.target_roles,
            "interests": profile.interests,
            "interest_evidence": [e.model_dump() for e in profile.evidence if e.kind == "other"],
        },
        SectorProposal,
    )
    if not result.sectors or any(
        not s.name.strip() or not s.rationale.strip() for s in result.sectors
    ):
        raise ValueError("The sector proposal needs named sectors with reasons.")
    result.sectors = _order_sector_proposal(result.sectors)
    return result


def confirm_search_brief(profile: Profile, draft: SearchBriefDraft) -> SearchBrief:
    if not profile.confirmed:
        raise ValueError("Confirm the candidate profile before the search brief.")
    validate_search_brief(profile, draft)
    return SearchBrief(
        **draft.model_dump(),
        profile_fingerprint=profile_fingerprint(profile),
        confirmed=True,
        confirmation_fingerprint=_confirmation_fingerprint(profile, draft),
    )


def require_confirmed_search_brief(profile: Profile) -> SearchBrief:
    brief = profile.search_brief
    if not profile.confirmed or not brief or not brief.confirmed:
        raise ValueError("Review and confirm your profile and search brief before discovery.")
    validate_search_brief(profile, brief)
    if brief.profile_fingerprint != profile_fingerprint(
        profile
    ) or brief.confirmation_fingerprint != _confirmation_fingerprint(profile, brief):
        raise ValueError(
            "Your profile or search brief changed. Review and confirm a new brief before discovery."
        )
    return brief
