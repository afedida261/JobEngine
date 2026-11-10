"""Opportunity cards and search-audit presentation."""

import pandas as pd
import streamlit as st


def render_job(job, run):
    title = job.facts.title if job.facts else job.discovery.title
    company = job.facts.company if job.facts else job.discovery.company
    with st.expander(
        f"{company} · {title}  |  {job.score:g}/100", expanded=job.category == "strong"
    ):
        st.link_button("Original job posting ↗", job.page.url)
        st.caption(f"Retrieved {job.page.retrieved_at} · {job.page.extraction_method}")
        if job.facts:
            st.write(
                f"Location: {job.facts.location or 'Not stated'} · Page availability signal: {job.facts.availability}"
            )
            if job.discovery.sector:
                st.caption("Sector: " + job.discovery.sector)
            for note in job.facts.extraction_notes:
                st.caption(note)
        for reason in job.reasons:
            st.write("• " + reason)
        if job.judgment:
            st.markdown("**Why your background may stand out**")
            evidence = {e.id: e for e in run.profile.evidence}
            requirements = {r.id: r for r in job.facts.requirements}
            if not job.judgment.differentiators:
                st.caption("No supported role-specific differentiating strength was identified.")
            for d in job.judgment.differentiators:
                st.write(d.explanation)
                st.caption("Employer: " + requirements[d.requirement_id].quote)
                for eid in d.evidence_ids:
                    st.caption(f"{eid} · Applicant: {evidence[eid].quote}")
            st.markdown("**Requirements and evidence**")
            rows = []
            for assessment in job.judgment.requirements:
                req = requirements[assessment.requirement_id]
                constraints_by_id = {c.id: c for c in run.profile.constraints}
                support = [f"{eid}: {evidence[eid].quote}" for eid in assessment.evidence_ids]
                support.extend(
                    f"{cid}: {constraints_by_id[cid].value}"
                    for cid in assessment.supporting_constraint_ids
                )
                rows.append(
                    {
                        "Requirement": req.text,
                        "Type": req.importance,
                        "Assessment": assessment.status,
                        "Reasoning": assessment.explanation,
                        "Employer quotation": req.quote,
                        "Candidate evidence": " | ".join(support),
                    }
                )
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            if job.judgment.constraints:
                constraints = {c.id: c for c in run.profile.constraints}
                st.markdown("**Constraints and unresolved eligibility**")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Constraint": constraints[c.constraint_id].field,
                                "Applicant": constraints[c.constraint_id].value,
                                "Status": c.status,
                                "Employer quotation": c.job_quote or "Not stated",
                                "Reasoning": c.explanation,
                            }
                            for c in job.judgment.constraints
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
            st.write(
                f"Interest alignment: {job.judgment.interest_alignment.level} — {job.judgment.interest_alignment.explanation}"
            )
            if job.judgment.interest_alignment.job_quote:
                st.caption("Employer: " + job.judgment.interest_alignment.job_quote)
            for uncertainty in job.judgment.uncertainties:
                st.write("Uncertainty: " + uncertainty)
        for issue in job.validation_issues:
            st.warning(issue)
        st.caption(
            f"Simple keyword overlap baseline: {job.keyword_score:g}% · Does not affect the AI ranking."
        )
        st.download_button(
            "Download retrieved source",
            data=job.page.text,
            file_name=f"source-{job.page.content_hash[:12] or 'unverified'}.txt",
            key=f"source-{run.id}-{run.jobs.index(job)}",
        )
        with st.expander("Source text and provenance"):
            st.caption("SHA-256 of retained text: " + job.page.content_hash)
            st.text(job.page.text or job.page.error)


def render_results(store):
    run = st.session_state.get("viewed_run") or st.session_state.get("run")
    if not run:
        st.info("Confirm a candidate profile and run discovery to see genuine opportunities here.")
    else:
        st.subheader("Your application shortlist")
        st.caption(f"Saved run {run.id[:8]} · {run.created_at}")
        if run.purpose == "validation":
            st.info(
                "Engineering validation run using the saved profile and a proposed sector plan. Manually review the listings before relying on the judgments."
            )
        all_inspected = run.inspected_jobs or run.jobs
        counts = {
            category: sum(j.category == category for j in all_inspected)
            for category in ["strong", "weaker", "needs_review", "excluded", "unverified"]
        }
        for col, (category, label) in zip(
            st.columns(5),
            [
                ("strong", "Strong"),
                ("weaker", "Weaker"),
                ("needs_review", "Needs review"),
                ("excluded", "Excluded"),
                ("unverified", "Unverified"),
            ],
        ):
            col.metric(label, counts[category])
        st.caption(
            "Counts cover all inspected postings; up to ten opportunities appear below. Scores order jobs within a category and are not interview probabilities. Grounded quotations support inspection, not a guarantee of correct interpretation."
        )
        if not counts["strong"]:
            st.warning(
                "No strong matches were established in this run. Any weaker opportunities are less convincing; unresolved eligibility is shown separately for review."
            )
        if not run.jobs:
            st.info(
                "No shortlist opportunities were established. Inspect the research trace and excluded or unverified sources below, then review the sector plan."
            )
        for warning in run.search.warnings:
            st.warning(warning)
        for category, label in [
            ("strong", "Strong matches"),
            ("weaker", "Weaker matches"),
            ("needs_review", "Potential stretches · eligibility needs review"),
            ("excluded", "Excluded · stated conflicts or essential gaps"),
            ("unverified", "Unverified · source or grounding could not be established"),
        ]:
            group = [j for j in run.jobs if j.category == category]
            if group:
                st.markdown(f"### {label}")
                for job in group:
                    render_job(job, run)
        st.download_button(
            "Export complete run and evidence",
            data=run.model_dump_json(indent=2),
            file_name=f"jobengine-{run.id[:8]}.json",
            mime="application/json",
        )
        with st.expander("Discovery coverage and sources"):
            st.caption(
                f"Search retrieved {run.search.retrieved_at}. The web search is not exhaustive, and search index dates are not application availability guarantees."
            )
            if run.search.stop_reason:
                st.write(run.search.summary)
                st.caption("Stop reason: " + run.search.stop_reason)
            if run.search.companies:
                st.markdown("**Companies checked**")
                for company in run.search.companies:
                    st.link_button(f"{company.sector} · {company.name}", company.official_url)
                    if company.early_career_evidence_url:
                        st.link_button(
                            "Early-career hiring clue (verify the actual posting)",
                            company.early_career_evidence_url,
                        )
            if run.search.steps:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Stage": s.stage,
                                "Sector": s.sector,
                                "Company": s.company or "",
                                "Queries": " | ".join(s.queries),
                                "Candidates": s.candidates,
                                "Outcome": s.outcome,
                            }
                            for s in run.search.steps
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
            if run.inspected_jobs:
                st.markdown("**All inspected postings, including exclusions and source failures**")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Company": j.facts.company if j.facts else j.discovery.company,
                                "Title": j.facts.title if j.facts else j.discovery.title,
                                "URL": j.discovery.url,
                                "Category": j.category,
                                "Reason": " ".join(j.reasons),
                                "Validation": " | ".join(j.validation_issues),
                            }
                            for j in run.inspected_jobs
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
            for source in run.search.sources[:40]:
                st.link_button(source["title"] or source["url"], source["url"])
            if len(run.search.sources) > 40:
                st.caption("The complete source list is retained in the run export.")
            st.text(run.search.query)
