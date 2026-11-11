"""Offline challenge-set audit; relevance labels are fixed before scoring.

Core = direct, paraphrased, acronym/alternate terminology, and verbose relevant roles.
Adjacent is plausible but not core. Senior jobs are occupationally relevant, separately
tagged because these fictional applicants have 0.5 professional years. Irrelevant jobs
include keyword traps. These are diagnostic fixtures, not representative recall estimates.
"""

import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from jobengine.models import (
    DiscoveryItem,
    Document,
    Evidence,
    IntakePreferences,
    JobPage,
    Profile,
    SearchBriefDraft,
    SearchStrength,
    SectorTarget,
)
from jobengine.onboarding.brief import confirm_search_brief
from jobengine.onboarding.preferences import intake_constraints
from jobengine.search import prescreen

CASES = {
    "software": {
        "name": "Ava Synthetic",
        "roles": ["Software engineer", "Backend developer"],
        "interests": "Reliable web services, distributed systems, APIs and databases",
        "evidence": "Created Python web services with PostgreSQL and automated tests.",
        "location": "San Francisco Bay Area",
        "alias": "San Francisco, CA",
        "direct": "Develop software and backend services. Build reliable web services, distributed systems, APIs and databases with Python and PostgreSQL.",
        "paraphrase": (
            "Application Programmer",
            "Implement server-side request handlers, HTTP endpoints, transactional storage and resilient deployments for online products.",
        ),
        "alternate": (
            "Platform Builder",
            "Ship server-side components in Golang, expose REST endpoints, maintain SQL persistence and troubleshoot production incidents.",
        ),
        "verbose": "Software Engineer - Payments Transaction Processing and Banking Infrastructure",
        "adjacent": (
            "Site Reliability Specialist",
            "Operate distributed systems and web services; automate deployments and diagnose production incidents.",
        ),
        "unrelated": (
            "Pastry Chef",
            "Bake sourdough loaves, prepare desserts and supervise kitchen sanitation.",
        ),
        "trap": (
            "Software Engineer Recruiter",
            "Sell staffing placements to software engineer and backend developer clients. Discuss reliable web services, distributed systems, APIs and databases with buyers. This is commission-based recruiting sales, not coding or implementation.",
        ),
        "negation": (
            "Backend Developer",
            "This advertisement sells training subscriptions to backend developer customers. Product copy lists reliable web services, distributed systems, APIs and databases. No software development duties: cold-call prospects and close subscription sales.",
        ),
    },
    "ml": {
        "name": "Mina Synthetic",
        "roles": ["Data scientist", "Machine learning engineer"],
        "interests": "Predictive modeling, experiments, statistical learning and forecasting",
        "evidence": "Trained a classifier and evaluated precision and recall using Python.",
        "location": "Washington DC",
        "alias": "Washington, DC",
        "direct": "Work as a data scientist and machine learning engineer on predictive modeling, experiments, statistical learning and forecasting. Train classifiers with Python and evaluate precision and recall.",
        "paraphrase": (
            "Applied Research Associate",
            "Fit probabilistic estimators, quantify uncertainty, run randomized trials and validate out-of-sample generalization on held-out observations.",
        ),
        "alternate": (
            "AI Researcher",
            "Fine-tune neural networks in PyTorch, construct benchmark suites and assess generalization of large language models.",
        ),
        "verbose": "Machine Learning Engineer - Probabilistic Demand Estimation and Experimentation Platforms",
        "adjacent": (
            "Quantitative Analyst",
            "Use statistical experiments, forecasting and Python to investigate uncertainty and explain numerical findings.",
        ),
        "unrelated": (
            "Dental Hygienist",
            "Clean teeth, take oral radiographs and teach patients brushing techniques.",
        ),
        "trap": (
            "Data Scientist Recruiter",
            "Recruit data scientist and machine learning engineer candidates. Advertise predictive modeling, experiments, statistical learning and forecasting expertise to clients. The work is staffing sales and account acquisition, not building models.",
        ),
        "negation": (
            "Machine Learning Engineer",
            "Sell an online course named Machine Learning Engineer. Marketing scripts mention data scientist work, predictive modeling, experiments, statistical learning and forecasting. No statistical analysis or model training is performed in this sales position.",
        ),
    },
    "journalism": {
        "name": "Jules Synthetic",
        "roles": ["Local news reporter", "Staff writer"],
        "interests": "Community reporting, education, culture, investigations and public interest journalism",
        "evidence": "Reported neighborhood stories and interviewed school-board members for a community newspaper.",
        "location": "NYC",
        "alias": "New York, NY",
        "direct": "Work as a local news reporter and staff writer. Cover community reporting, education, culture, investigations and public interest journalism. Report neighborhood stories and interview school-board members.",
        "paraphrase": (
            "City Hall Correspondent",
            "Attend council meetings, interview residents, check public records and file accurate articles about municipal decisions.",
        ),
        "alternate": (
            "Civic Affairs Correspondent",
            "Produce sourced accounts of municipal policy, verify officials' claims and explain school-board decisions to neighborhood readers.",
        ),
        "verbose": "Local News Reporter - Municipal Accountability Schools and Neighborhood Affairs",
        "adjacent": (
            "Editorial Research Assistant",
            "Support community investigations, verify sources and research education and culture for public interest journalism.",
        ),
        "unrelated": (
            "Payroll Accountant",
            "Reconcile wages, process invoices and maintain vendor ledgers.",
        ),
        "trap": (
            "Local News Reporter Recruiter",
            "Sell staffing services to local news reporter and staff writer clients. Pitch community reporting, education, culture, investigations and public interest journalism talent. The position is recruiter sales, not gathering or publishing news.",
        ),
        "negation": (
            "Staff Writer",
            "Sell subscriptions to a product named Staff Writer. Promotional copy mentions local news reporter work, community reporting, education, culture, investigations and public interest journalism. No reporting, interviewing or article writing duties.",
        ),
    },
    "nursing": {
        "name": "Nia Synthetic",
        "roles": ["Registered nurse", "Clinical nurse"],
        "interests": "Patient care, clinical assessment, medication safety and acute care",
        "evidence": "Completed supervised bedside care and medication administration in an adult medical unit.",
        "location": "Minneapolis-Saint Paul",
        "alias": "Twin Cities",
        "direct": "Work as a registered nurse and clinical nurse. Deliver patient care, clinical assessment, medication safety and acute care. Perform supervised bedside care and medication administration in an adult medical unit.",
        "paraphrase": (
            "Bedside Clinician",
            "Monitor vital signs, administer prescribed therapies, coordinate discharge plans and support adults recovering after surgery.",
        ),
        "alternate": (
            "RN - Med/Surg",
            "Provide bedside monitoring, administer prescribed medicines, document observations and coordinate discharge for hospitalized adults. An RN license is required.",
        ),
        "verbose": "Registered Nurse - Adult Medical Surgical Ward and Discharge Coordination",
        "adjacent": (
            "Care Coordination Associate",
            "Support patient care transitions, medication safety, discharge planning and clinical assessment documentation.",
        ),
        "unrelated": (
            "Industrial Welder",
            "Fabricate steel frames, inspect weld seams and operate metalworking equipment.",
        ),
        "trap": (
            "Registered Nurse Recruiter",
            "Recruit registered nurse and clinical nurse candidates. Discuss patient care, clinical assessment, medication safety and acute care with hospital clients. This is staffing sales with no bedside or patient treatment responsibilities.",
        ),
        "negation": (
            "Clinical Nurse",
            "Sell subscriptions to Clinical Nurse training videos. Sales scripts reference registered nurse skills, patient care, clinical assessment, medication safety and acute care. This position does not deliver treatment or bedside care.",
        ),
    },
}


def profile_for(spec, *, years=0.5, roles=None, interests=None, location=None):
    roles = roles or spec["roles"]
    quote = spec["evidence"]
    intake = IntakePreferences(
        graduation_status="not_applicable",
        locations=[location or spec["location"]],
        work_arrangements=["on_site", "hybrid", "remote"],
    )
    profile = Profile(
        documents=[Document(id="D1", label="Synthetic source", text=quote)],
        evidence=[Evidence(id="E1", kind="other", summary=quote, quote=quote, source_id="D1")],
        target_roles=roles,
        interests=interests or spec["interests"],
        professional_years=years,
        intake=intake,
        constraints=intake_constraints(intake),
        confirmed=True,
    )
    draft = SearchBriefDraft(
        summary="Synthetic approved search plan",
        target_roles=roles,
        work_themes=["Synthetic goals"],
        strengths=[SearchStrength(statement=quote, evidence_ids=["E1"])],
        search_queries=["Synthetic query"],
        avoid=[],
        stretch_policy="Review adjacent roles without asserting qualifications.",
        unresolved_questions=[],
        sectors=[
            SectorTarget(
                name="Synthetic sector", relationship="preferred", rationale="Fictional audit"
            )
        ],
    )
    profile.search_brief = confirm_search_brief(profile, draft)
    return profile


def pair(identity, title, body, location):
    item = DiscoveryItem(
        title=title, company="Fictional employer", url=f"https://example.com/{identity}"
    )
    text = f"Location: {location}\n\nJOB DESCRIPTION\n{body}"
    return item, JobPage(
        requested_url=item.url,
        url=item.url,
        retrieved_at="2026-11-10T00:00:00+00:00",
        text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        extraction_method="Synthetic audit fixture",
        status="retrieved",
    )


def labeled_jobs(name, spec):
    rows = [
        ("direct", spec["roles"][0], spec["direct"], "core"),
        ("paraphrase", *spec["paraphrase"], "core"),
        ("alternate", *spec["alternate"], "core"),
        ("verbose", spec["verbose"], spec["direct"], "core"),
        ("adjacent", *spec["adjacent"], "adjacent"),
        ("unrelated", *spec["unrelated"], "irrelevant"),
        ("trap", *spec["trap"], "irrelevant"),
        ("negation", *spec["negation"], "irrelevant"),
        (
            "senior",
            "Senior " + spec["roles"][0],
            spec["direct"]
            + " Requires twelve years of professional tenure and leading a department.",
            "senior",
        ),
        (
            "staff",
            ("Senior " if spec["roles"][-1].startswith("Staff ") else "Staff ") + spec["roles"][-1],
            spec["direct"]
            + " Requires ten years of professional tenure and setting organizational strategy.",
            "senior",
        ),
    ]
    return {
        f"{name}-{key}": {
            "label": label,
            "pair": pair(f"{name}-{key}", title, body, spec["location"]),
        }
        for key, title, body, label in rows
    }


def measure(rows, labels, n, accepted):
    relevant = {key for key, value in labels.items() if value in accepted}
    retrieved = {row["item"].url.rsplit("/", 1)[-1] for row in rows[:n]}
    hits = len(relevant & retrieved)
    return {"hits": hits, "relevant": len(relevant), "recall": hits / len(relevant)}


def serialize(rows, labels):
    return [
        {
            "id": row["item"].url.rsplit("/", 1)[-1],
            "title": row["item"].title,
            "label": labels.get(row["item"].url.rsplit("/", 1)[-1], "added_irrelevant"),
            "rank": row["rank"],
            "score": row["score"],
            "breakdown": row["breakdown"],
            "status": row["status"],
        }
        for row in rows
    ]


def audit_case(name, spec):
    profile = profile_for(spec)
    jobs = labeled_jobs(name, spec)
    labels = {key: value["label"] for key, value in jobs.items()}
    pool = [value["pair"] for value in jobs.values()]
    _, rows = prescreen.prioritize_postings(pool, profile, top_n=5)
    metrics = {
        str(n): {
            "core": measure(rows, labels, n, {"core"}),
            "core_and_adjacent": measure(rows, labels, n, {"core", "adjacent"}),
            "all_same_field": measure(rows, labels, n, {"core", "adjacent", "senior"}),
            "terminology": measure(
                rows,
                {key: "terminology" for key in (f"{name}-paraphrase", f"{name}-alternate")},
                n,
                {"terminology"},
            ),
            "irrelevant_selected": sum(
                labels[row["item"].url.rsplit("/", 1)[-1]] == "irrelevant" for row in rows[:n]
            ),
            "senior_selected": sum(
                labels[row["item"].url.rsplit("/", 1)[-1]] == "senior" for row in rows[:n]
            ),
        }
        for n in (3, 5, 10)
    }
    clean_pool = [
        value["pair"]
        for key, value in jobs.items()
        if value["label"] in {"core", "adjacent"} or key.endswith("-unrelated")
    ]
    _, clean_rows = prescreen.prioritize_postings(clean_pool, profile, top_n=3)
    clean = {
        str(n): {
            "core": measure(clean_rows, labels, n, {"core"}),
            "terminology": measure(
                clean_rows,
                {key: "terminology" for key in (f"{name}-paraphrase", f"{name}-alternate")},
                n,
                {"terminology"},
            ),
        }
        for n in (3, 5)
    }

    additions = [
        pair(
            f"{name}-neutral-{i}",
            "Warehouse Packing Associate",
            "Stack pallets, pack cartons, inspect freight and operate loading docks.",
            spec["location"],
        )
        for i in range(50)
    ]
    _, expanded = prescreen.prioritize_postings(pool + additions, profile, top_n=5)
    original_scores = {row["item"].url: row["score"] for row in rows}
    original_expanded = [row for row in expanded if row["item"].url in original_scores]
    neutral = {
        "added_jobs": len(additions),
        "max_original_score_change": round(
            max(abs(row["score"] - original_scores[row["item"].url]) for row in original_expanded),
            2,
        ),
        "original_order_changed": [r["item"].url for r in original_expanded]
        != [r["item"].url for r in rows],
        "recall_at_5": measure(expanded, labels, 5, {"core"}),
        "ranked": serialize(original_expanded, labels),
    }
    recruiter_jobs = [
        pair(f"{name}-recruiter-{i}", *spec["trap"], spec["location"]) for i in range(20)
    ]
    _, recruiter_rows = prescreen.prioritize_postings(pool + recruiter_jobs, profile, top_n=10)
    expanded_labels = labels | {f"{name}-recruiter-{i}": "irrelevant" for i in range(20)}
    recruiter = {
        "added_jobs": 20,
        "metrics": {
            str(n): {
                "core": measure(recruiter_rows, expanded_labels, n, {"core"}),
                "terminology": measure(
                    recruiter_rows,
                    {key: "terminology" for key in (f"{name}-paraphrase", f"{name}-alternate")},
                    n,
                    {"terminology"},
                ),
            }
            for n in (5, 10)
        },
        "ranked": serialize(recruiter_rows, expanded_labels),
    }
    _, reversed_rows = prescreen.prioritize_postings(list(reversed(pool)), profile, top_n=1)

    title_pool = [
        pair(f"{name}-title-{i}", title, spec["direct"], spec["location"])
        for i, title in enumerate([spec["roles"][0], spec["roles"][0].split()[-1], spec["verbose"]])
    ]
    _, titles = prescreen.prioritize_postings(title_pool, profile, top_n=3)
    senior_pool = [
        pair(f"{name}-seniority-{i}", prefix + spec["roles"][0], spec["direct"], spec["location"])
        for i, prefix in enumerate(["Junior ", "Senior ", "Staff "])
    ]
    _, seniority = prescreen.prioritize_postings(senior_pool, profile, top_n=3)
    _, experienced = prescreen.prioritize_postings(
        senior_pool, profile_for(spec, years=15), top_n=3
    )
    alias_pool = [
        pair(f"{name}-location-exact", spec["roles"][0], spec["direct"], spec["location"]),
        pair(f"{name}-location-alias", spec["roles"][0], spec["direct"], spec["alias"]),
    ]
    _, aliases = prescreen.prioritize_postings(alias_pool, profile, top_n=2)
    return {
        "candidate": spec["name"],
        "roles": spec["roles"],
        "interests": spec["interests"],
        "professional_years": profile.professional_years,
        "fixtures": [
            {
                "id": key,
                "label": value["label"],
                "title": value["pair"][0].title,
                "source_text": value["pair"][1].text,
            }
            for key, value in jobs.items()
        ],
        "metrics": metrics,
        "clean_pool": {
            "size": len(clean_pool),
            "metrics": clean,
            "ranked": serialize(clean_rows, labels),
        },
        "ranked": serialize(rows, labels),
        "neutral_board_expansion": neutral,
        "recruiter_board_expansion": recruiter,
        "reverse_order_top_one": serialize(reversed_rows[:1], labels),
        "title_probe": serialize(titles, {}),
        "seniority_probe": serialize(seniority, {}),
        "experience_changes_scores": [r["score"] for r in seniority]
        != [r["score"] for r in experienced],
        "location_probe": {
            "preferred": spec["location"],
            "equivalent_label": spec["alias"],
            "ranked": serialize(aliases, {}),
        },
    }


def corpus_shift_probe():
    spec = CASES["software"]
    profile = profile_for(spec, roles=["Software engineer"], interests="Databases, APIs")
    original = [
        pair(
            "db",
            "Software Engineer",
            "Maintain relational databases and durable storage using Python for migrations.",
            spec["location"],
        ),
        pair("api", "Software Engineer", "Implement APIs and endpoint routing.", spec["location"]),
    ]
    additions = [
        pair(
            f"warehouse-{i}",
            "Warehouse Clerk",
            "Track packing labels in inventory databases; load pallets and sort freight.",
            spec["location"],
        )
        for i in range(50)
    ]
    _, baseline = prescreen.prioritize_postings(original, profile, top_n=1)
    _, expanded = prescreen.prioritize_postings(original + additions, profile, top_n=1)
    return {
        "added_jobs": 50,
        "before": serialize(baseline, {"db": "core", "api": "core"}),
        "after": serialize(
            [r for r in expanded if r["item"].url in {p[0].url for p in original}],
            {"db": "core", "api": "core"},
        ),
    }


@pytest.fixture(autouse=True)
def no_network_or_models(monkeypatch):
    from jobengine.infrastructure.llm import ModelClient

    def fail(*args, **kwargs):
        pytest.fail("Audit must stay offline and deterministic")

    monkeypatch.setattr(httpx.Client, "send", fail)
    monkeypatch.setattr(ModelClient, "structured", fail)
    monkeypatch.setattr(ModelClient, "search", fail)


@pytest.fixture(scope="module")
def audit():
    return {
        "algorithm_sha256": hashlib.sha256(Path(prescreen.__file__).read_bytes()).hexdigest(),
        "cases": {name: audit_case(name, spec) for name, spec in CASES.items()},
        "corpus_shift": corpus_shift_probe(),
    }


@pytest.mark.parametrize("name", CASES)
def test_labeled_recall_measurements_are_deterministic_and_preserve_all_jobs(audit, name):
    result = audit["cases"][name]
    repeated = audit_case(name, CASES[name])
    assert result == repeated
    assert len(result["ranked"]) == 10
    assert sum(r["status"] == "not_selected_for_batch" for r in result["ranked"]) == 5
    assert result["metrics"]["10"]["core"]["recall"] == 1
    assert result["metrics"]["10"]["core_and_adjacent"]["recall"] == 1
    assert all(0 <= m["core"]["recall"] <= 1 for m in result["metrics"].values())


def test_report_is_emitted_only_when_explicitly_requested(audit):
    destination = os.getenv("JOBENGINE_PRESCREEN_AUDIT_DIR")
    if destination:
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / "audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")


@pytest.mark.parametrize("name", CASES)
def test_missing_source_details_remain_auditable_without_false_rejection(name):
    spec = CASES[name]
    posting = pair("unknown", spec["roles"][0], "", "")
    selected, rows = prescreen.prioritize_postings([posting], profile_for(spec), top_n=1)
    assert selected == [posting] and len(rows) == 1
    assert rows[0]["breakdown"]["location_signal"] == 5
    assert rows[0]["status"] == "selected_for_evaluation"


@pytest.mark.parametrize("name", CASES)
@pytest.mark.xfail(
    strict=True,
    reason="Known semantic-recall gap: paraphrased relevant roles miss the top-5 cutoff",
)
def test_at_least_one_terminology_variant_survives_top_five(audit, name):
    assert audit["cases"][name]["metrics"]["5"]["terminology"]["hits"] >= 1


@pytest.mark.parametrize("name", CASES)
@pytest.mark.xfail(
    strict=True,
    reason="Title precision alone gives a generic single noun the same 45 points as the complete role",
)
def test_specific_role_title_has_more_signal_than_generic_noun(audit, name):
    rows = audit["cases"][name]["title_probe"]
    specific = next(row for row in rows if row["id"].endswith("-0"))
    generic = next(row for row in rows if row["id"].endswith("-1"))
    assert specific["breakdown"]["title_roles"] > generic["breakdown"]["title_roles"]


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(
            "software",
            marks=pytest.mark.xfail(strict=True, reason="Bay Area city labels are not normalized"),
        ),
        pytest.param(
            "ml",
            marks=pytest.mark.xfail(
                strict=True, reason="DC punctuation aliases are not normalized"
            ),
        ),
        "journalism",
        pytest.param(
            "nursing",
            marks=pytest.mark.xfail(strict=True, reason="Twin Cities aliases are not normalized"),
        ),
    ],
)
def test_equivalent_location_labels_get_equal_location_signal(audit, name):
    rows = audit["cases"][name]["location_probe"]["ranked"]
    assert rows[0]["breakdown"]["location_signal"] == rows[1]["breakdown"]["location_signal"]
