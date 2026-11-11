"""Small offline geographic vocabulary, not a geocoder or commute/metro inference.

US state/region names follow the four Census regions. City aliases identify the
same city only; unlisted or ambiguous places stay unknown for conflict detection.
"""

import re

from jobengine.grounding import normalized

REGIONS = {
    "northeast": "CT:Connecticut|ME:Maine|MA:Massachusetts|NH:New Hampshire|RI:Rhode Island|VT:Vermont|NJ:New Jersey|NY:New York|PA:Pennsylvania",
    "midwest": "IN:Indiana|IL:Illinois|MI:Michigan|OH:Ohio|WI:Wisconsin|IA:Iowa|KS:Kansas|MN:Minnesota|MO:Missouri|NE:Nebraska|ND:North Dakota|SD:South Dakota",
    "south": "DE:Delaware|DC:District of Columbia|FL:Florida|GA:Georgia|MD:Maryland|NC:North Carolina|SC:South Carolina|VA:Virginia|WV:West Virginia|AL:Alabama|KY:Kentucky|MS:Mississippi|TN:Tennessee|AR:Arkansas|LA:Louisiana|OK:Oklahoma|TX:Texas",
    "west": "AZ:Arizona|CO:Colorado|ID:Idaho|MT:Montana|NV:Nevada|NM:New Mexico|UT:Utah|WY:Wyoming|AK:Alaska|CA:California|HI:Hawaii|OR:Oregon|WA:Washington",
}
STATES = {
    normalized(name): code.lower()
    for values in REGIONS.values()
    for pair in values.split("|")
    for code, name in [pair.split(":")]
}
REGIONS = {
    region: {pair.split(":")[0].lower() for pair in values.split("|")}
    for region, values in REGIONS.items()
}
# Geographic aliases, independent of applicant/job/industry. No metro expansion.
CITIES = {
    "new york": ("ny", ("nyc", "new york city")),
    "san francisco": ("ca", ("sf",)),
    "los angeles": ("ca", ()),
    "washington": (
        "dc",
        (
            "washington dc",
            "washington d c",
        ),
    ),
    "boston": ("ma", ()),
    "chicago": ("il", ()),
    "seattle": ("wa", ()),
    "austin": ("tx", ()),
    "dallas": ("tx", ()),
    "atlanta": ("ga", ()),
    "miami": ("fl", ()),
    "denver": ("co", ()),
    "philadelphia": ("pa", ()),
    "portland": ("or", ()),
}


def _clean(value):
    return " ".join(re.findall(r"\w+", normalized(value)))


def place(value):
    """Return (canonical name, state, kind); unknown names support equality only."""
    value = _clean(value)
    if re.search(
        r"\b(commuting|distance|near|within|greater|area|metro|including|such as)\b", value
    ):
        return value, None, "unknown"
    for city, (state, aliases) in CITIES.items():
        if value in aliases or value == f"{city} {state}":
            return city, state, "city"
    if value in STATES or value in STATES.values():
        state = STATES.get(value, value)
        return state, state, "state"
    if value in REGIONS:
        return value, None, "region"
    for name, code in sorted(STATES.items(), key=lambda x: -len(x[0])):
        for suffix in (name, code):
            if value.endswith(" " + suffix):
                return value[: -len(suffix)].strip(), code, "city"
    if value in CITIES and value not in {"washington", "portland", "new york"}:
        return value, CITIES[value][0], "city"
    return value, None, "unknown"


def place_key(value):
    name, state, kind = place(value)
    return f"{kind}:{name}:{state}" if kind != "unknown" else name


def compatible(allowed, required):
    """True/False only with known spatial containment/disjointness, else None."""
    a, sa, ka = place(allowed)
    r, sr, kr = place(required)
    if (a, sa, ka) == (r, sr, kr):
        return True
    if kr == "region" and sa:
        return sa in REGIONS[r]
    if ka == "region" and sr:
        return sr in REGIONS[a]
    if sa and sr:
        if sa != sr:
            return False
        if "state" in {ka, kr}:
            return True
        if a == r:
            return True
        return False if a in CITIES and r in CITIES else None
    return None


def restriction_match(allowed, required):
    results = [compatible(a, r) for a in allowed for r in required]
    if any(result is True for result in results):
        return True
    if results and all(result is False for result in results):
        return False
    return None
