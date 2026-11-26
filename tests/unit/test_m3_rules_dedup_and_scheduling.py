from datetime import UTC, datetime, timedelta
from time import perf_counter

import pytest

from relayforge.core.rich_text import CanonicalRichText, FormatSpanUtf16, utf16_len
from relayforge.core.rules import (
    BrandSpec,
    FilterContext,
    RuleError,
    ThompsonNFA,
    check_route_dag,
    compile_filter,
    duplicate_decision,
    evaluate_filter,
    fingerprint,
    inject_brand_identity,
    resolve_policy,
    transform,
)
from relayforge.core.scheduling import catch_up, scheduled_for


@pytest.mark.parametrize("expression", ["(a+)+$", "([a-zA-Z]+)*$"])
def test_thompson_nfa_redos_bound(expression):
    matcher = ThompsonNFA(expression)
    start = perf_counter()
    assert not matcher.search("a" * 5000 + "!", anchored=True)
    elapsed = (perf_counter() - start) * 1000
    print(f"Thompson NFA complete in {elapsed:.2f} ms")
    assert elapsed < 50


@pytest.mark.parametrize(("pattern", "text", "expected"), [
    ("^a(b|c)+d?$", "AcBBd", True),
    ("^a(b|c)+d?$", "xxaBC", False),
    ("[a-z]+", "🔥HELLO", True),
    (r"x\+y", "zzx+y", True),
    ("[^a-c]$", "D", True),
    ("^.$", "\n", False),
    ("^a*?b$", "ab", None),
    (r"(x)\1", "xx", None),
    ("(?=b)", "b", None),
    ("[x", "x", None),
    ("a" * 257, "", None),
])
def test_thompson_nfa_supported_syntax(pattern, text, expected):
    if expected is None:
        with pytest.raises(RuleError):
            ThompsonNFA(pattern, ignore_case=True)
    else:
        assert ThompsonNFA(pattern, ignore_case=True).search(text) is expected


def test_filter_access_and_complexity():
    leaf = {"op": "has_link"}
    tree = {"op": "ALL", "children": [
        {"op": "ANY", "children": [{"op": "keyword_any", "value": ["خبر"]}, leaf]},
        {"op": "NOT", "children": [{"op": "has_mention"}]},
        {"op": "min_utf16_len", "value": 3},
    ]}
    filter_ = compile_filter(tree)
    text = CanonicalRichText("خبر 🔥")
    assert evaluate_filter(filter_, FilterContext(text)) == (True, "FILTER_MATCH")
    assert evaluate_filter(filter_, FilterContext(
        text, access_class="PROTECTED_NO_FORWARD",
    )) == (False, "PROTECTED_NO_FORWARD")
    assert evaluate_filter(filter_, FilterContext(
        text, access_class="REFERENCE_ONLY", platform="EITAA",
    )) == (False, "REFERENCE_ONLY_INCOMPATIBLE")
    nested = leaf
    for _ in range(12):
        nested = {"op": "NOT", "children": [nested]}
    for invalid in (nested, {"op": "ALL", "children": [leaf] * 64}):
        with pytest.raises(RuleError, match="COMPLEXITY"):
            compile_filter(invalid)


def test_transform_brand_precedence_and_span_safety():
    input_ = CanonicalRichText("🔥 خبر @ads داغ", (FormatSpanUtf16(0, 6, "BOLD"),))
    transformed = transform(input_, {
        "prefix": "🔔 ", "strip_third_party_mentions": True,
        "replacements": {"خبر": "تازه"},
    })
    assert transformed.plain_text == "🔔 🔥 تازه  داغ"
    assert transformed.spans_utf16 == (FormatSpanUtf16(utf16_len("🔔 "), 3, "BOLD"),)
    a = inject_brand_identity(transformed, BrandSpec("امضای الف", "HEADER", "—"))
    b = inject_brand_identity(transformed, BrandSpec("امضای ب", "FOOTER", "—"))
    assert a.plain_text.startswith("امضای الف") and b.plain_text.endswith("امضای ب")
    assert inject_brand_identity(a, BrandSpec("امضای الف", "HEADER", "—")) == a
    assert resolve_policy({"prefix": "W", "suffix": "W"}, {"prefix": "R"},
                          {"prefix": "D"}) == {"prefix": "D", "suffix": "W"}


def test_duplicate_and_cycles():
    exact, sim = fingerprint("خبر مهم امروز در شهر")
    assert fingerprint("  خبر مهم امروز\tدر شهر  ") == (exact, sim)
    assert -2**63 <= sim < 2**63
    assert duplicate_decision((exact, sim), [(exact, sim)]) == "DUPLICATE_EXACT_SKIP"
    assert duplicate_decision((exact, sim), [
        ("other", sim ^ 1),
    ]) == "AMBIGUOUS_NEAR_DUPLICATE_HOLD_REVIEW"
    assert duplicate_decision((exact, sim), [("other", sim ^ 127)]) == "UNIQUE_PASS"
    check_route_dag([("A", "B"), ("A", "C"), ("B", "C")])
    for cycle in [[("A", "B"), ("B", "A")], [("A", "B"), ("B", "C"), ("C", "A")]]:
        with pytest.raises(RuleError, match="ROUTE_CYCLE_DETECTED"):
            check_route_dag(cycle)


class Clock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value


def test_scheduler_tehran_midnight_dst_and_modes():
    now = datetime(2026, 11, 26, 20, 20, tzinfo=UTC)
    assert scheduled_for({"mode": "DAILY_AT", "time": "00:10"}, "Asia/Tehran",
                         clock=Clock(now)) == datetime(2026, 11, 26, 20, 40, tzinfo=UTC)
    assert scheduled_for({"mode": "WEEKLY_AT", "time": "00:10", "weekday": 4},
                         "Asia/Tehran", clock=Clock(now)) == now + timedelta(minutes=20)
    assert scheduled_for({"mode": "INTERVAL_MINUTES", "interval_minutes": 15}, "UTC",
                         clock=Clock(now)) == now + timedelta(minutes=15)
    gap = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
    assert scheduled_for({"mode": "DAILY_AT", "time": "02:30"}, "America/New_York",
                         clock=Clock(gap)) == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    explicit = {"mode": "AT_TIMESTAMP", "at_timestamp": "2026-11-26T23:50:00+03:30"}
    assert scheduled_for(explicit, "Asia/Tehran", clock=Clock(now)) == now
    overdue = now - timedelta(minutes=5)
    assert catch_up(overdue, now) == ("NEEDS_REVIEW", None)
    assert catch_up(overdue, now, "SKIP_EXPIRED") == ("SKIP_EXPIRED", None)
    assert catch_up(overdue, now, "GRADUAL_THROTTLED", spacing_sec=30,
                    position=2) == ("SCHEDULE", now + timedelta(minutes=1))
