from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from jobengine.infrastructure.llm import (
    DEFAULT_INTERVIEW_MODEL,
    DEFAULT_SEARCH_MODEL,
    PRICES,
    ModelClient,
)
from jobengine.infrastructure.storage import Store
from jobengine.onboarding.brief import require_confirmed_search_brief
from jobengine.search.pipeline import run_search
from jobengine.ui.onboarding import render_onboarding
from jobengine.ui.results import render_results
from jobengine.ui.review import render_review
from jobengine.ui.state import persist_progress, restore_progress_once, saved_progress_controls


def show_error(exc: Exception):
    # SDK errors can contain request details; keep secrets and candidate material
    # out of the error UI. Grounding/value errors are our own actionable messages.
    if isinstance(exc, (ValueError, RuntimeError)):
        st.error(str(exc))
    else:
        st.error(
            f"{type(exc).__name__}: the operation did not complete. Check API access, billing, and the network, then retry. No replacement results were generated."
        )


def main():
    ROOT = Path(__file__).resolve().parents[2]
    load_dotenv(ROOT / ".env", override=True)
    st.set_page_config(
        page_title="JobEngine · Evidence before applications", page_icon="◎", layout="wide"
    )
    st.markdown(
        """<style>
    .block-container {max-width:1200px;padding-top:2rem}
    h1 {letter-spacing:-.04em} h2 {letter-spacing:-.025em}
    div[data-testid="stMetric"] {border:1px solid #dbe4e4;border-radius:10px;padding:12px}
    .eyebrow {color:#39746b;letter-spacing:.17em;font-size:.73rem;font-weight:700}
    [data-testid="InputInstructions"] {display:none}
    </style>""",
        unsafe_allow_html=True,
    )

    try:
        store = Store(
            Path(os.getenv("JOBENGINE_DATA_DIR", str(ROOT / "data"))),
            float(os.getenv("JOBENGINE_BUDGET_USD", "10")),
        )
    except ValueError as exc:
        st.error(f"Invalid local budget setting: {exc}")
        st.stop()

    def get_client() -> ModelClient:
        return ModelClient(
            st.session_state.get("api_key", "") or os.getenv("OPENAI_API_KEY", ""),
            store,
            st.session_state["model"],
            interview_model=st.session_state["interview_model"],
        )

    try:
        restore_progress_once(store)
    except (ValueError, RuntimeError) as exc:
        show_error(exc)
        st.stop()

    with st.sidebar:
        st.markdown("### ◎ JobEngine")
        st.caption("A smaller list. A stronger reason to apply.")
        models = list(PRICES)
        default_interview_model = os.getenv("OPENAI_INTERVIEW_MODEL", DEFAULT_INTERVIEW_MODEL)
        st.selectbox(
            "Interview model",
            models,
            index=models.index(default_interview_model)
            if default_interview_model in models
            else models.index(DEFAULT_INTERVIEW_MODEL),
            key="interview_model",
        )
        default_model = os.getenv("OPENAI_SEARCH_MODEL", DEFAULT_SEARCH_MODEL)
        st.selectbox(
            "Search model",
            models,
            index=models.index(default_model)
            if default_model in models
            else models.index(DEFAULT_SEARCH_MODEL),
            key="model",
        )
        st.caption("Interview: low reasoning · Search and job assessment: low reasoning")
        st.text_input(
            "API key (optional session override)",
            type="password",
            key="api_key",
            help="Otherwise reads OPENAI_API_KEY from the local .env file. The key is not saved by the app.",
        )
        configured = bool(st.session_state.api_key or os.getenv("OPENAI_API_KEY"))
        st.caption(
            "API key configured" if configured else "Add OPENAI_API_KEY to .env to enable AI calls."
        )
        usage = store.usage()
        st.metric(
            "Local budget used / reserved",
            f"${usage['total']:.3f}",
            help="Conservative estimates plus reservations for ambiguous failed requests. Not an account-wide billing limit.",
        )
        st.progress(
            min(1.0, usage["total"] / usage["budget"]),
            text=f"${usage['budget']:.2f} development budget",
        )
        with st.expander("Usage details"):
            st.caption(
                "Estimates include a cache-write allowance. Failed requests retain their reservation. Cached results spend nothing."
            )
            if usage["calls"]:
                st.dataframe(
                    pd.DataFrame(usage["calls"])[
                        ["operation", "model", "amount", "state", "searches"]
                    ],
                    hide_index=True,
                )
        st.divider()
        st.caption(
            "Resumes and evidence are sent to the selected model provider. Role, interest, and location text is used for web discovery. Local caches and exports contain candidate data; they stay in the ignored data/ folder."
        )
        saved_progress_controls(store)
        if st.button("Start a new candidate", width="stretch"):
            store.clear_active_onboarding()
            for key in list(st.session_state):
                if key not in {"model", "interview_model", "api_key"}:
                    del st.session_state[key]
            st.rerun()

    st.markdown(
        '<div class="eyebrow">JOB DISCOVERY / GROUNDED IN YOUR WORK</div>', unsafe_allow_html=True
    )
    st.title("Find your reason to stand out.")
    st.write(
        "Discover real openings, inspect the evidence, and decide where an application deserves your time."
    )
    st.caption("1  Background → 2  Conversation → 3  Profile → 4  Search brief → 5  Discovery")
    profile_tab, results_tab, review_tab = st.tabs(
        ["Onboarding & discovery", "Opportunities", "Independent review"]
    )

    with profile_tab:
        render_onboarding(get_client, show_error, configured, store)
        profile = st.session_state.get("profile")
        ready = False
        if profile and profile.search_brief:
            try:
                require_confirmed_search_brief(profile)
                ready = st.session_state.onboarding.stage == "ready" and bool(
                    profile.search_brief.sectors
                )
            except ValueError:
                pass
        if ready:
            st.divider()
            st.subheader("5. Discover opportunities using your approved brief")
            st.caption(
                "Search coverage is incomplete. We retrieve source pages before evaluating; blocked pages remain unverified. A small run can take several minutes."
            )
            with st.form("search_form"):
                st.caption(
                    "We search sector by sector and show up to ten opportunities. Search and inspection limits keep each run bounded."
                )
                search_clicked = st.form_submit_button(
                    "Discover & evaluate jobs", type="primary", disabled=not configured
                )
            if search_clicked:
                try:
                    with st.status("Discovering jobs…", expanded=True) as status:
                        progress_message = st.empty()
                        run = run_search(get_client(), profile, progress=progress_message.write)
                        st.session_state.run = run
                        st.session_state.pop("viewed_run", None)
                        persist_progress(store)
                        status.update(
                            label=f"Run complete · {len(run.inspected_jobs)} inspected · {len(run.jobs)} opportunities",
                            state="complete",
                            expanded=False,
                        )
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
            if st.session_state.get("run"):
                st.success("Results are ready in the Opportunities tab.")
        else:
            st.info(
                "Discovery stays locked until you review and confirm your profile and search brief."
            )

    with results_tab:
        render_results(store)
    with review_tab:
        render_review(store)
