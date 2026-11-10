"""Independent human-review form and comparison presentation."""

import json

import streamlit as st

from jobengine.ui.state import persist_progress


def render_review(store):
    st.subheader("Judge the jobs before reading the AI")
    st.write(
        "Spend about ten minutes deciding which jobs deserve an application. For a blind comparison, use this tab before opening Opportunities. Read the original source, then record your judgment."
    )
    run = st.session_state.get("viewed_run") or st.session_state.get("run")
    if not run:
        st.info("A completed discovery run supplies the real listings for review.")
    else:
        # Source order deliberately differs from model rank; no AI judgments shown.
        by_url = {j.discovery.url: j for j in run.jobs}
        choices = [
            "Not reviewed",
            "Worth applying",
            "Possible stretch",
            "Do not apply",
            "Cannot determine",
        ]
        labels = []
        with st.form(f"human_review_{run.id}"):
            review_items = run.review_items()
            for index, item in enumerate(review_items):
                st.markdown(f"**{index + 1}. {item.company} · {item.title}**")
                st.link_button("Read original listing", item.url)
                label = st.radio(
                    "Your judgment", choices, horizontal=True, key=f"review-{run.id}-{index}"
                )
                note = st.text_input(
                    "Reason or unsupported claim found", key=f"note-{run.id}-{index}"
                )
                unsupported = st.checkbox(
                    "I found an unsupported claim in the model assessment (after comparing)",
                    key=f"unsupported-{run.id}-{index}",
                )
                labels.append(
                    {
                        "url": item.url,
                        "label": label,
                        "note": note,
                        "unsupported_claim": unsupported,
                    }
                )
            compare = st.form_submit_button("Save judgments & compare")
        if compare:
            scored = [
                label
                for label in labels
                if label["label"] != "Not reviewed" and label["url"] in by_url
            ]
            strong = [label for label in scored if by_url[label["url"]].category == "strong"]
            precision = (
                sum(label["label"] == "Worth applying" for label in strong) / len(strong)
                if strong
                else None
            )
            missed = [
                label
                for label in scored
                if label["label"] == "Worth applying"
                and by_url[label["url"]].category not in {"strong", "weaker"}
            ]
            report = {
                "run_id": run.id,
                "labels": labels,
                "reviewed": len(scored),
                "strong_match_precision": precision,
                "worthwhile_jobs_outside_shortlist": len(missed),
                "unsupported_claims_flagged": sum(label["unsupported_claim"] for label in scored),
            }
            store.save_run("review" + run.id, report)
            st.session_state.review_report = report
            persist_progress(store)
        report = st.session_state.get("review_report")
        if report and report["run_id"] == run.id:
            st.write(
                f"Reviewed: {report['reviewed']} · Worthwhile jobs outside shortlist: {report['worthwhile_jobs_outside_shortlist']} · Unsupported claims flagged: {report['unsupported_claims_flagged']}"
            )
            precision = report["strong_match_precision"]
            st.write(
                "Strong-match precision: "
                + (
                    f"{precision:.0%}"
                    if precision is not None
                    else "Not measurable without reviewed strong matches"
                )
            )
            st.caption(
                "These results describe this small reviewed sample only. A keyword baseline is available on each opportunity for comparison."
            )
            st.download_button(
                "Export human review",
                json.dumps(report, indent=2),
                file_name="human-review.json",
                mime="application/json",
            )
