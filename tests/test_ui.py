from pathlib import Path

from jobengine.infrastructure.storage import Store


def test_initial_interface_has_no_fabricated_results(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest

    # This renders the actual Streamlit script without a model call.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = AppTest.from_file(
        Path(__file__).resolve().parents[1] / "jobengine" / "ui" / "legacy_app.py",
        default_timeout=30,
    ).run()
    assert not app.exception
    assert app.title[0].value == "Find your reason to stand out."
    assert any("genuine opportunities" in info.value for info in app.info)


def test_profile_and_result_views_render_all_evidence(case):
    from streamlit.testing.v1 import AppTest

    from jobengine.models import Run, SearchRecord
    from jobengine.search.ranking import rank_job

    profile, item, *_ = case
    app = AppTest.from_file(
        Path(__file__).resolve().parents[1] / "jobengine" / "ui" / "legacy_app.py",
        default_timeout=30,
    )
    app.session_state["profile"] = profile
    app.session_state["run"] = Run(
        id="syntheticuitest",
        model="synthetic",
        profile=profile,
        search=SearchRecord(
            query="Synthetic test",
            retrieved_at="2026-11-07",
            summary="",
            sources=[],
            jobs=[item],
            warnings=[],
        ),
        jobs=[rank_job(*case)],
    )
    app.run()
    assert not app.exception
    assert any("Requirements and evidence" in m.value for m in app.markdown)
    assert any("Synthetic company" in exp.label for exp in app.expander)


def test_saved_results_open_without_importing_candidate_or_spending(case, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest

    from jobengine.models import Run, SearchRecord
    from jobengine.search.ranking import rank_job

    profile, item, *_ = case
    profile.documents[0].label = "Test fixture account"
    store = Store(os.environ["JOBENGINE_DATA_DIR"])
    run = Run(
        id="testviewresults",
        model="synthetic",
        profile=profile,
        purpose="validation",
        search=SearchRecord(
            query="Synthetic test",
            retrieved_at="2026-11-07",
            summary="",
            sources=[],
            jobs=[item],
            warnings=[],
        ),
        jobs=[rank_job(*case)],
    )
    store.save_run(run.id, run.model_dump(mode="json"))
    app = AppTest.from_file(
        Path(__file__).resolve().parents[1] / "jobengine" / "ui" / "legacy_app.py",
        default_timeout=30,
    ).run()
    before = store.load_onboarding()
    app.selectbox(key="previous_run_choice").select(run.id).run()
    next(b for b in app.button if b.label == "View saved results").click().run()
    assert not app.exception
    assert app.session_state["viewed_run"].id == run.id
    assert any("Synthetic company" in exp.label for exp in app.expander)
    assert store.load_onboarding() == before
    assert store.usage()["total"] == 0
