import hashlib
import hmac
import json
import math
import re
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from statistics import median

from relayforge.core.rich_text import SafePlaceholderTemplate
from relayforge.core.security import scrub_sensitive_text

WRITING = {
    "tone": "neutral",
    "formality": "SEMI_FORMAL",
    "avg_sentence_words": 12,
    "emoji_density": "LOW",
    "hashtag_count_range": [0, 3],
    "opening_pattern": "",
    "closing_cta_pattern": "",
    "instructions_fa": "",
}
LAYOUT = {"template_tokens": "{body}", "bold_first_line": False}
EVIDENCE = {"unique_sample_count": 0, "evidence_sufficiency": "INSUFFICIENT"}
IDENTITY = re.compile(r"(?<!\w)@[\w]+|https?://[^\s<>]+")
SECRET_NAME = re.compile(r"(api.?key|token|password|authorization|credential|secret)", re.I)


def digest(data):
    return hashlib.sha256(
        json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def metrics(text):
    return {
        "characters": len(text),
        "lines": len(text.splitlines()),
        "paragraphs": len([part for part in re.split(r"\n\s*\n", text) if part.strip()]),
        "emoji": sum(unicodedata.category(char) == "So" for char in text),
        "hashtags": len(re.findall(r"(?<!\w)#\w+", text)),
        "links": len(re.findall(r"https?://", text)),
        "cta": bool(re.search(r"دنبال کنید|عضو شوید|subscribe|follow", text, re.I)),
    }


def analyze_samples(rows, requested, threshold=10):
    unique = {}
    for row in rows:
        normalized = " ".join(unicodedata.normalize("NFC", row["plain_text"]).split()).casefold()
        if normalized:
            unique.setdefault(normalized, row)
    groups = {"SHORT": [], "LONG": []}
    for row in unique.values():
        groups["SHORT" if len(row["plain_text"]) <= 180 else "LONG"].append(row)
    for group in groups.values():
        group.sort(key=lambda item: digest(str(item["id"])))
    ordered = []
    while any(groups.values()) and len(ordered) < requested:
        for label, group in groups.items():
            if group and len(ordered) < requested:
                ordered.append((group.pop(), label))
    counters = Counter()
    media = Counter()
    identity = Counter()
    footers = Counter()
    partitioned = []
    observed_metrics = []
    for index, (row, label) in enumerate(ordered):
        partition = "EVALUATION" if index % 5 == 4 else "EXTRACTION"
        partitioned.append((row["id"], partition, label))
        if partition == "EVALUATION":
            continue
        text = row["plain_text"].strip()
        counters[label] += 1
        identity.update(set(IDENTITY.findall(text)))
        last = text.splitlines()[-1]
        if IDENTITY.search(last) or metrics(last)["cta"]:
            footers[last[:300]] += 1
        media["WITH_MEDIA" if row.get("media") else "TEXT_ONLY"] += 1
        observed_metrics.append(metrics(text))
    count = len(ordered)
    stats = {
        "requested_sample_count": requested,
        "fetched_count": len(rows),
        "unique_sample_count": count,
        "shortfall_reason": ("INSUFFICIENT_DISTINCT_SAMPLES" if count < requested else None),
        "evidence_sufficiency": "SUFFICIENT" if count >= threshold else "LOW_EVIDENCE",
        "clusters": dict(counters),
        "metadata_only": dict(media),
        "distributions": {
            key: dict(Counter(str(item[key]) for item in observed_metrics)) for key in metrics("")
        },
        "median_char_length": int(median(item["characters"] for item in observed_metrics))
        if observed_metrics
        else 0,
    }
    return (
        stats,
        {
            "references": sorted(key for key, n in identity.items() if n >= 2),
            "footers": sorted(key for key, n in footers.items() if n >= 2),
        },
        partitioned,
    )


def strip_identity(text, identity=None):
    for footer in (identity or {}).get("footers", []):
        text = text.replace(footer, "")
    references = set((identity or {}).get("references", []))
    return IDENTITY.sub(lambda match: "" if match[0] in references else match[0], text).strip()


def _check(value, rule):
    if "const" in rule and value != rule["const"]:
        raise ValueError("STYLE_CONSTANT_MISMATCH")
    if "enum" in rule and value not in rule["enum"]:
        raise ValueError("STYLE_INVALID_ENUM")
    kind = rule.get("type")
    match kind:
        case "object":
            if not isinstance(value, dict):
                raise ValueError("STYLE_INVALID_OBJECT")
            props = rule["properties"]
            if set(rule.get("required", ())) - value.keys() or (
                rule.get("additionalProperties") is False and value.keys() - props.keys()
            ):
                raise ValueError("STYLE_INVALID_PROPERTIES")
            for key, item in value.items():
                _check(item, props[key])
        case "array":
            if not isinstance(value, list) or not rule.get("minItems", 0) <= len(value) <= (
                rule.get("maxItems", 10000)
            ):
                raise ValueError("STYLE_INVALID_ARRAY")
            for item in value:
                _check(item, rule["items"])
        case "integer" | "number":
            if (
                type(value) not in (int, float)
                or (kind == "integer" and type(value) is not int)
                or not math.isfinite(value)
                or not rule.get("minimum", -math.inf) <= value <= (rule.get("maximum", math.inf))
            ):
                raise ValueError("STYLE_INVALID_NUMBER")
        case "boolean":
            if type(value) is not bool:
                raise ValueError("STYLE_INVALID_BOOLEAN")
        case "string":
            if not isinstance(value, str) or not rule.get("minLength", 0) <= len(value) <= (
                rule.get("maxLength", 10000)
            ):
                raise ValueError("STYLE_INVALID_STRING")
            if "pattern" in rule and not re.fullmatch(
                rule["pattern"].removeprefix("^").removesuffix("$"), value
            ):
                raise ValueError("STYLE_INVALID_PATTERN")
            if rule.get("format") == "date-time" and (
                "T" not in value or datetime.fromisoformat(value).utcoffset() is None
            ):
                raise ValueError("STYLE_INVALID_TIMESTAMP")
            if (
                scrub_sensitive_text(value) != value
                or re.search(
                    r"<\s*(?:script|iframe|object)|\b(?:javascript|data|file):",
                    value,
                    re.I,
                )
                or re.search(r"(?i)(?:api.?key|password|secret|token)\s*[:=]\s*\S+", value)
            ):
                raise ValueError("STYLE_UNSAFE_VALUE")


def validate_package(package, schema):
    _check(package, schema)
    body = {key: value for key, value in package.items() if key != "integrity_sha256"}
    if not hmac.compare_digest(digest(body), package["integrity_sha256"]):
        raise ValueError("STYLE_INTEGRITY_MISMATCH")
    revision = package["revision"]
    SafePlaceholderTemplate(revision["layout_template"]["template_tokens"])
    lo, hi = revision["writing_rules"]["hashtag_count_range"]
    if lo > hi:
        raise ValueError("STYLE_INVALID_HASHTAG_RANGE")


def make_package(profile, revision, schema):
    package = {
        "package_format": "RELAYFORGE_STYLE_PACKAGE",
        "schema_version": "2.0",
        "exported_at": datetime.now(UTC).isoformat(),
        "style_profile": profile,
        "revision": revision,
    }
    package["integrity_sha256"] = digest(package)
    validate_package(package, schema)
    return package


def compose(styles):
    if not 1 <= len(styles) <= 5 or any(
        type(weight) not in (int, float) or not math.isfinite(weight) or weight <= 0
        for _, weight in styles
    ):
        raise ValueError("INVALID_COMPOSITION_WEIGHTS")
    total = math.fsum(weight for _, weight in styles)
    weights = [weight / total for _, weight in styles]
    dominant = max(range(len(styles)), key=weights.__getitem__)
    base = styles[dominant][0]["revision"]
    writing = dict(base["writing_rules"])
    writing["avg_sentence_words"] = math.fsum(
        package["revision"]["writing_rules"]["avg_sentence_words"] * weight
        for (package, _), weight in zip(styles, weights, strict=True)
    )
    writing["hashtag_count_range"] = [
        round(
            math.fsum(
                package["revision"]["writing_rules"]["hashtag_count_range"][i] * weight
                for (package, _), weight in zip(styles, weights, strict=True)
            )
        )
        for i in (0, 1)
    ]
    return base | {
        "revision_no": 1,
        "writing_rules": writing,
        "composition_weights": [
            {"source_slug": package["style_profile"]["slug"], "weight": weight}
            for (package, _), weight in zip(styles, weights, strict=True)
        ],
    }


def edit_signals(before, after):
    prior, edited = metrics(before), metrics(after)
    signals = set()
    for key in ("paragraphs", "hashtags", "emoji"):
        if prior[key] != edited[key]:
            signals.add(f"{'REDUCE' if prior[key] > edited[key] else 'INCREASE'}_{key.upper()}")
    if len(after) < len(before) * 0.8:
        signals.add("SHORTEN_TEXT")
    if prior["cta"] and not edited["cta"]:
        signals.add("REMOVE_CTA")
    return sorted(signals)
