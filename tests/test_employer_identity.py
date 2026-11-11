"""Employer relationships must be grounded before qualification assessment."""

import pytest

from jobengine.search.pipeline import evaluate


@pytest.mark.parametrize(
    "extracted", ["The New York Times Company", "The New York Times Company (Wirecutter)"]
)
def test_greenhouse_legal_name_and_brand_reach_assessment(case, extracted):
    profile, item, page, facts, judgment = case
    item.company = "The New York Times"
    item.url = page.url = page.requested_url = (
        "https://job-boards.greenhouse.io/thenewyorktimes/jobs/123"
    )
    item.company_source_url = (
        "https://boards-api.greenhouse.io/v1/boards/thenewyorktimes/jobs?content=true"
    )
    page.extraction_method = (
        "Greenhouse Job Board API; board_token=thenewyorktimes; posting_id=123; visible HTML"
    )
    page.text += (
        " Wirecutter is seeking an intern. The New York Times Company provides accommodations."
    )
    facts.company = extracted
    calls = []

    class Client:
        def structured(self, operation, *args):
            calls.append(operation)
            return facts if operation == "extract_job" else judgment

    result = evaluate(Client(), profile, item, page=page)
    assert "evaluate_fit" in calls, result.reasons


@pytest.mark.parametrize("api", [False, True], ids=["web", "api"])
@pytest.mark.parametrize(
    "discovered,extracted,description,accepted",
    [
        ("Acme", "Acme Company", "Acme Company is seeking an analyst.", True),
        ("Acme", "Acme (Research)", "Acme is seeking staff. Department: Research\n", True),
        ("Acme", "Bright Labs", "Bright Labs is a subsidiary of Acme.", True),
        ("Bright Labs", "Acme", "Bright Labs is a subsidiary of Acme.", True),
        ("Acme", "Bright Labs", "Bright Labs is a division of Acme.", True),
        ("Acme", "Bright Labs", "Bright Labs is a brand of Acme.", True),
        ("Acme", "Bright Labs", "Acme is also known as Bright Labs.", True),
        ("Acme", "Bright Labs", "Acme works with client Bright Labs.", False),
        ("Acme", "Bright Labs", "Acme competes with Bright Labs.", False),
        ("Acme", "Bright Labs", "Bright Labs is not a subsidiary of Acme.", False),
        ("Acme", "Bright Labs", "Bright Labs is a subsidiary of Acme Holdings.", False),
        ("Acme", "Bright Labs", "Bright Labs is a subsidiary of Acme's competitor.", False),
        (
            "Acme",
            "Acme (Bright Labs)",
            "Acme is seeking staff. Bright Labs is an unrelated employer.",
            False,
        ),
        ("Acme", "Acme Technologies", "Acme Technologies is seeking staff.", False),
        ("Acme", "Acme Holdings", "Acme Holdings is seeking staff.", False),
        ("Acme", "Acme (Bright Labs)", "Acme is seeking staff. Our client is Bright Labs.", False),
        ("Acme", "Bright Labs", "Bright Labs is seeking staff.", False),
        ("Acme", "Acme (Invented)", "Acme is seeking staff.", False),
    ],
)
def test_shared_relationship_policy(case, api, discovered, extracted, description, accepted):
    from jobengine.infrastructure.diagnostics import observe

    profile, item, page, facts, judgment = case
    item.company = discovered
    token = discovered.lower().replace(" ", "")
    item.url = page.url = page.requested_url = f"https://job-boards.greenhouse.io/{token}/jobs/123"
    item.company_source_url = (
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        if api
        else f"https://{token}.example/about"
    )
    page.extraction_method = (
        f"Greenhouse Job Board API; board_token={token}; posting_id=123; visible HTML"
        if api
        else "visible HTML"
    )
    page.text += "\n" + description
    facts.company = extracted
    calls, events = [], []

    class Client:
        def structured(self, operation, *args):
            calls.append(operation)
            return facts if operation == "extract_job" else judgment

    with observe(lambda stage, outcome, **details: events.append((stage, outcome, details))):
        result = evaluate(Client(), profile, item, page=page)
    assert ("evaluate_fit" in calls) == accepted, result.reasons
    decision = next(e for e in events if e[0] == "employer_identity")
    assert decision[1] == ("accepted" if accepted else "rejected")
    assert decision[2]["reason"]
    if not accepted:
        assert result.category == "unverified"
        assert "Employer identity mismatch" in result.reasons[0]


def test_columbia_university_knight_institute(case):
    from jobengine.search.employer_identity import validate_employer_identity

    _, item, page, _, _ = case
    item.company = "Columbia University – Knight First Amendment Institute"
    item.company_source_url = "https://knightcolumbia.org/"
    item.url = page.url = page.requested_url = (
        "https://opportunities.columbia.edu/jobs/communications"
    )
    page.text = (
        "Department: Knight First Amendment Institute\n"
        "The Knight First Amendment Institute defends free expression. "
        "Columbia University is committed to hiring qualified applicants."
    )
    decision = validate_employer_identity(item, page, "Columbia University")
    assert decision.accepted, decision.reason


@pytest.mark.parametrize(
    "mutation", ["wrong_board", "wrong_id", "wrong_host", "caller_only", "unrelated_mapping"]
)
def test_board_provenance_is_not_a_blanket_identity_bypass(case, mutation):
    from jobengine.search.employer_identity import validate_employer_identity

    _, item, page, _, _ = case
    item.company = "Acme"
    item.company_source_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    item.url = page.url = page.requested_url = "https://job-boards.greenhouse.io/acme/jobs/123"
    page.extraction_method = (
        "Greenhouse Job Board API; board_token=acme; posting_id=123; visible HTML"
    )
    page.text = "Employer (provided by caller): Acme\nAcme Company is seeking staff."
    if mutation == "wrong_board":
        item.company_source_url = item.company_source_url.replace("/acme/", "/other/")
    elif mutation == "wrong_id":
        page.extraction_method = page.extraction_method.replace("posting_id=123", "posting_id=456")
    elif mutation == "wrong_host":
        item.company_source_url = item.company_source_url.replace(
            "greenhouse.io", "greenhouse.io.attacker.example"
        )
    elif mutation == "caller_only":
        page.text = "Employer (provided by caller): Acme Company\nNo verified employer name."
    else:
        item.company = "Arbitrary Employer"
        page.text += " Arbitrary Employer is seeking staff."
    assert not validate_employer_identity(item, page, "Acme Company").accepted


@pytest.mark.parametrize(
    "discovered,extracted,description,accepted",
    [
        (
            "Harbor",
            "Learning Institute (Harbor)",
            "Company: Learning Institute\nHarbor participates in E-Verify.",
            True,
        ),
        (
            "Northstar",
            "Northstar / Beacon",
            "At Northstar and Beacon, you'll collaborate with our teams.",
            True,
        ),
        (
            "Northstar",
            "Northstar / Beacon / Other",
            "At Northstar and Beacon, you'll collaborate. Our client Other helps us.",
            False,
        ),
        (
            "Acme",
            "Bright Brands, LLC",
            "Company: Bright Brands, LLC\nAcme is owner of a brand portfolio. We engage fans through our brands, including Bright, Spark, and Glow.",
            True,
        ),
        (
            "Acme",
            "Bright Brands, LLC",
            "Company: Bright Brands, LLC\nAcme is not owner of a brand portfolio. We engage fans through our brands, including Bright, Spark, and Glow.",
            False,
        ),
        (
            "Acme",
            "Bright Brands, LLC",
            "Company: Bright Brands, LLC\nAcme works with brands including Bright.",
            False,
        ),
        (
            "Acme",
            "Acme Company",
            "Employer (provided by caller): Acme Company\nNo independent name evidence.",
            False,
        ),
    ],
)
def test_organizational_evidence_without_employer_allowlists(
    case, discovered, extracted, description, accepted
):
    from jobengine.search.employer_identity import validate_employer_identity

    _, item, page, _, _ = case
    item.company = discovered
    item.company_source_url = f"https://{discovered.lower()}.example/"
    page.text = description
    decision = validate_employer_identity(item, page, extracted)
    assert decision.accepted == accepted, decision.reason
