"""Pure helpers for jev_fastmcp.py, ported from @jkudish/jev-mcp 0.8.0:
dist/lib.js in full, plus the answer validators and the patch-review
projection from dist/index.js. No engine access; everything here is a
deterministic function of its inputs.

Upstream is MIT-licensed (Copyright (c) Joey Kudish); the jev_review /
jev_gate question design is itself adapted upstream from burnigtm/jev-mcp
(MIT) via PR #2 by rimusz. See LICENSES/jev-mcp.txt.

Deliberate difference from upstream: string lengths (limits, truncation) are
counted in Unicode code points, where JavaScript counts UTF-16 code units.
The two agree except for characters outside the Basic Multilingual Plane
(emoji, some rare CJK), which count as 1 here and 2 upstream.
"""
import json
import math
import re
from decimal import ROUND_HALF_UP, Decimal

# ── Limits ────────────────────────────────────────────────────────────────────
MAX_CANDIDATES = 250
MAX_CANDIDATE_CHARS = 2000
# Float-safe tolerance for probability-sum checks (a 0.99 sum can compare
# greater than 0.01 away from 1 in IEEE-754).
PROBABILITY_SUM_TOLERANCE = 0.01 + 1e-12
MAX_CLASSES = 250
MAX_ITEMS = 64
MAX_ITEM_CHARS = 2000
MAX_CANDIDATES_DECIDE = 6
MAX_REQUIREMENTS = 3
MAX_RERANK_CANDIDATES = 250
MAX_RERANK_TOTAL_CHARS = 100_000
MAX_PROPOSITIONS = 64
MAX_PROPOSITION_CHARS = 2000
MAX_NOUL_TOTAL_CHARS = 150_000
MAX_COMPARE_ASPECTS = 10
MAX_EXTRACT_FIELDS = 32
MAX_EXTRACT_CANDIDATES = 20
MAX_EXTRACT_CANDIDATE_CHARS = 2_000
MAX_EXTRACT_TOTAL_CHARS = 50_000
REGEX_TIMEOUT_MS = 1_000
MAX_GATE_CLAIMS = 16
MAX_GATE_EVIDENCE_ITEMS = 16
MAX_GATE_EVIDENCE_CHARS = 200_000
MAX_REVIEW_DOC_CHARS = 50_000
MAX_CLAIM_CHARS = 2_000
# |score - sum(i * p_i)| tolerance for a score answer (two-decimal rounding
# envelope 0.005 + 0.015, plus the same float guard as above).
SCORE_MEAN_TOLERANCE = 0.02 + 1e-12
DEFAULT_COMPOSITE_FLOOR = 0.7

# ── Fixed criteria ────────────────────────────────────────────────────────────
RELATION_TO_VERDICT = {
    "supports": "verified",
    "contradicts": "contradicted",
    "says_nothing": "unsupported",
}
DECIDE_ESCAPE_HATCHES = {
    "ask_user": "A consequential user preference or requirement is missing; ask instead of inventing it",
    "investigate": "Gather missing technical or factual evidence before selecting a candidate",
    "none": "None of the supplied candidates fits the known requirements",
}
COMPARE_RELATIONS = {
    "same_fact": "Both passages state the same underlying fact or claim",
    "contradicts": "The passages state opposing facts about the same subject",
    "different_facts": "The passages discuss different subjects or make non-overlapping claims",
}
ASPECT_RELATIONS = {
    "same_fact": "Both passages make comparable assertions about this aspect and they agree",
    "contradicts": "Both passages address this aspect and their assertions conflict",
    "different_facts": "The passages do not both make a comparable assertion about this aspect: at least one does not address it, or their mentions do not overlap",
}
REVIEW_WEIGHTS = {
    "correctness": 0.4,
    "spec_match": 0.3,
    "test_gap": 0.15,
    "blast_radius": 0.15,
}
VERIFY_CLAIM_CRITERIA = {
    "verified": "The evidence clearly supports the claim",
    "contradicted": "The evidence contradicts the claim",
    "unsupported": "The evidence neither supports nor contradicts the claim",
}
SCORE_OPTION_KEYS = ["0", "1", "2"]


# ── JavaScript-compatible primitives ──────────────────────────────────────────
def is_record(value):
    """A JSON object: present, non-null, not an array."""
    return isinstance(value, dict)


def is_finite_number(value):
    """typeof value === "number" && Number.isFinite(value); bool is not a number."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def to_fixed(value, digits):
    """Number.prototype.toFixed for non-negative values: round half up on the
    exact binary value (Python's round() is half-to-even)."""
    return str(Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def fixed_number(value, digits):
    """Number(value.toFixed(digits))."""
    return float(to_fixed(value, digits))


def js_number_str(value):
    """String(number) as JavaScript prints it: integral values without '.0',
    fixed notation from 1e-6 up to 1e21."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    if 1e-6 <= abs(value) < 1e21:
        return format(Decimal(repr(value)), "f")
    mantissa, _, exponent = repr(value).partition("e")
    return f"{mantissa}e{int(exponent):+d}" if exponent else repr(value)


def _is_array_index(key):
    return key.isascii() and key.isdigit() and (key == "0" or not key.startswith("0")) and int(key) < 2**32 - 1


def js_object(mapping):
    """The key order a JavaScript object gives the same entries: integer-like
    keys ("0", "42") first in ascending order, then the rest in insertion
    order. It decides option letters wherever caller ids become keys."""
    index_keys = sorted((k for k in mapping if _is_array_index(k)), key=int)
    return {k: mapping[k] for k in index_keys + [k for k in mapping if not _is_array_index(k)]}


def js_json_normalize(value):
    """What a JSON round trip through JavaScript does to numbers: integral
    floats become integers (JS has one number type, so 1.0 prints as 1)."""
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() and abs(value) < 1e21 else value
    if isinstance(value, list):
        return [js_json_normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: js_json_normalize(v) for k, v in value.items()}
    return value


def js_stringify(value, _level=0):
    """JSON.stringify(value, null, 2): json.dumps(indent=2)'s layout, with
    numbers printed as JavaScript prints them and non-finite numbers as null."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return js_number_str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    pad = "  " * (_level + 1)
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        return "[\n" + ",\n".join(pad + js_stringify(v, _level + 1) for v in value) + "\n" + "  " * _level + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        body = ",\n".join(pad + json.dumps(str(k), ensure_ascii=False) + ": " + js_stringify(v, _level + 1) for k, v in value.items())
        return "{\n" + body + "\n" + "  " * _level + "}"
    raise TypeError(f"not JSON-serializable: {type(value).__name__}")


_JS_SPACE_CLASS = "[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*"
_JS_NUMBER = re.compile(
    rf"^{_JS_SPACE_CLASS}(?:(?P<hex>0[xX][0-9a-fA-F]+)|(?P<oct>0[oO][0-7]+)|(?P<bin>0[bB][01]+)"
    rf"|(?P<inf>[+-]?Infinity)|(?P<dec>[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?))?{_JS_SPACE_CLASS}$")


def js_number(text):
    """Number(text) for a string: None where JavaScript gives NaN."""
    m = _JS_NUMBER.match(text)
    if m is None:
        return None
    if m.group("hex"):
        return int(m.group("hex"), 16)
    if m.group("oct"):
        return int(m.group("oct")[2:], 8)
    if m.group("bin"):
        return int(m.group("bin")[2:], 2)
    if m.group("inf"):
        return -math.inf if m.group("inf").startswith("-") else math.inf
    if m.group("dec"):
        return float(m.group("dec"))
    return 0  # empty or whitespace only


# String.prototype.trim removes WhiteSpace (TAB, VT, FF, ZWNBSP, category Zs)
# and LineTerminator (LF, CR, LS, PS) -- not Python's wider isspace() set.
_JS_TRIM_CHARS = "\t\n\v\f\r   " + "".join(chr(c) for c in range(0x2000, 0x200B)) + "    　﻿"


def js_trim(text):
    return text.strip(_JS_TRIM_CHARS)


# ── lib.js ────────────────────────────────────────────────────────────────────
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_EDGE_UNDERSCORES = re.compile(r"^_+|_+$")


def sanitize_id(id_):
    """Keep alphanumerics, underscore, dash and dot; collapse the rest."""
    cleaned = _EDGE_UNDERSCORES.sub("", _UNSAFE_ID_CHARS.sub("_", id_))
    return cleaned[:64] if cleaned else ""


def ensure_unique_ids(items, fallback_prefix):
    """Ensure ids exist, are safe, and are unique; returns (items, renamed)."""
    used = set()
    renamed = {}
    out = []
    for i, item in enumerate(items):
        raw = item.get("id")
        raw = "" if raw is None else raw
        base = sanitize_id(raw) or f"{fallback_prefix}{i}"
        id_ = base
        n = 1
        while id_ in used:
            id_ = f"{base}_{n}"
            n += 1
        used.add(id_)
        if raw and raw != id_:
            renamed[raw] = id_
        new_item = dict(item)
        new_item["id"] = id_
        out.append(new_item)
    return out, renamed


def truncate(text, max_chars):
    """Truncate long text with an explicit marker so the model knows it is partial."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + " […truncated]"


def verify_action(confidence, auto_accept):
    return "auto" if confidence >= auto_accept else "review"


def screen_recommendation(injection, substance, relevance, block_at, review_at):
    if injection >= block_at:
        return {"action": "block", "reason": f"injection probability {to_fixed(injection, 2)} >= block threshold {js_number_str(block_at)}"}
    if injection >= review_at:
        return {"action": "review", "reason": f"injection probability {to_fixed(injection, 2)} >= review threshold {js_number_str(review_at)}"}
    if substance is not None and substance < 0.3:
        return {"action": "skip", "reason": f"little substantive content (substance {to_fixed(substance, 2)})"}
    if relevance is not None and relevance < 0.3:
        return {"action": "skip", "reason": f"not relevant to the stated purpose (relevance {to_fixed(relevance, 2)})"}
    return {"action": "pass", "reason": "no signals above thresholds"}


def exists_verdict(exists, found=0.7, absent=0.35):
    if exists >= found:
        return "answered"
    return "absent" if exists < absent else "partial"


def rank_candidates(candidates, probabilities):
    """Rank by probability, descending; ties keep caller order."""
    rows = []
    for index, candidate in enumerate(candidates):
        p = probabilities.get(candidate["id"])
        rows.append((dict(candidate, probability=0 if p is None else p), index))
    rows.sort(key=lambda row: (-row[0]["probability"], row[1]))
    return [candidate for candidate, _ in rows]


def margin_of(probabilities):
    """Winner-to-runner-up gap; a lone probability has no runner-up, so 0."""
    ranked = sorted((probabilities or {}).values(), reverse=True)
    if len(ranked) < 2:
        return 0
    return ranked[0] - ranked[1]


def classification_decision(top_probability, margin, auto_accept, minimum_margin):
    return "auto" if top_probability >= auto_accept and margin >= minimum_margin else "review"


def contradicts_recommendation(checks, recommended):
    return [c["requirement"] for c in checks if c["candidate"] == recommended and c["answer"] == "contradicted"]


def rerank_by_score(candidates, scores):
    """Sort by relevance, descending; stable, so ties keep caller order."""
    rows = [dict(c, relevance=scores[i] if i < len(scores) else 0) for i, c in enumerate(candidates)]
    return sorted(rows, key=lambda c: -c["relevance"])


def validate_policy_thresholds(auto_accept, review_at):
    if (not is_finite_number(auto_accept) or not is_finite_number(review_at)
            or auto_accept < 0 or auto_accept > 1 or review_at < 0 or review_at > 1
            or review_at > auto_accept):
        raise ValueError("Thresholds must satisfy 0 <= review_at <= auto_accept <= 1.")


def resolve_policy_thresholds(auto_accept=0.8, review_at=None):
    """Fill an omitted review_at so a lone low auto_accept cannot invert the pair."""
    resolved = min(0.5, auto_accept) if review_at is None else review_at
    validate_policy_thresholds(auto_accept, resolved)
    return auto_accept, resolved


def require_complete_context(action, truncated):
    """Truncated input never permits auto, only stronger actions."""
    return "review" if truncated and action == "auto" else action


def review_composite(correctness, spec_match, test_gap, blast_radius):
    """Weighted 0..1 composite from 0..2 rubric scores (test gap and blast radius inverted)."""
    def clamp01(v):
        return min(1, max(0, v))

    def clamp02(v):
        return min(2, max(0, v))
    return (REVIEW_WEIGHTS["correctness"] * clamp01(clamp02(correctness) / 2)
            + REVIEW_WEIGHTS["spec_match"] * clamp01(clamp02(spec_match) / 2)
            + REVIEW_WEIGHTS["test_gap"] * clamp01(1 - clamp02(test_gap) / 2)
            + REVIEW_WEIGHTS["blast_radius"] * clamp01(1 - clamp02(blast_radius) / 2))


def review_action(composite, safe_to_apply, min_confidence, auto_accept, review_at, composite_floor):
    """Unknown confidence escalates; auto needs safe_to_apply, min confidence
    and the composite all at their thresholds; otherwise review."""
    if min_confidence is None or min_confidence < review_at or safe_to_apply < review_at:
        return "escalate"
    if safe_to_apply >= auto_accept and composite >= composite_floor and min_confidence >= auto_accept:
        return "auto"
    return "review"


def claim_action(verdict, confidence, auto_accept, review_at):
    if confidence is None or confidence < review_at:
        return "escalate"
    if verdict == "contradicted" and confidence >= auto_accept:
        return "escalate"
    return "auto" if verdict == "verified" and confidence >= auto_accept else "review"


def worst_action(actions):
    if "escalate" in actions:
        return "escalate"
    if "review" in actions:
        return "review"
    return "auto"


def normalize_evidence(raw):
    """{id,text} items with unique ids, the same shape jev_verify uses."""
    if isinstance(raw, str):
        items = [{"id": "evidence", "text": raw}]
    elif isinstance(raw, list):
        items = raw
    else:
        items = [raw]
    return ensure_unique_ids(items, "evidence")[0]


def has_non_empty_evidence(items):
    return any(len(js_trim(item["text"])) > 0 for item in items)


# ── Answer validators (index.js) ──────────────────────────────────────────────
def _confidence_or_none(answer):
    c = answer.get("confidence")
    return c if is_finite_number(c) and 0 <= c <= 1 else None


def validate_choice_answer(answer, expected_keys):
    """Exact candidate keys, finite [0,1] probabilities summing to one, and a
    choice tied for the maximum probability. Returns {choice, probabilities,
    confidence} or None."""
    if not is_record(answer) or not isinstance(answer.get("choice"), str) or not is_record(answer.get("probabilities")):
        return None
    expected = set(expected_keys)
    probabilities = answer["probabilities"]
    keys = list(probabilities)
    values = list(probabilities.values())
    if (answer["choice"] not in expected or len(keys) != len(expected)
            or not all(k in expected for k in keys)
            or not all(is_finite_number(p) and 0 <= p <= 1 for p in values)
            or abs(sum(values) - 1) > PROBABILITY_SUM_TOLERANCE
            or probabilities[answer["choice"]] < max(values) - 1e-9):
        return None
    return {"choice": answer["choice"], "probabilities": probabilities, "confidence": _confidence_or_none(answer)}


def validate_noul_answer(answer):
    """A finite probability in [0,1], or None."""
    if not is_record(answer):
        return None
    p = answer.get("noul")
    return p if is_finite_number(p) and 0 <= p <= 1 else None


def validate_score_answer(answer):
    """A finite 0..2 score; when a distribution is present, exactly the keys
    0/1/2 summing to one with an expected value matching the score."""
    if not is_record(answer):
        return None
    score = answer.get("score")
    if not is_finite_number(score) or score < 0 or score > 2:
        return None
    confidence = _confidence_or_none(answer)
    probabilities = answer.get("probabilities")
    if probabilities is None:
        return {"score": score, "confidence": confidence, "probabilities": None}
    if not is_record(probabilities):
        return None
    keys = list(probabilities)
    values = list(probabilities.values())
    if (len(keys) != len(SCORE_OPTION_KEYS) or not all(k in SCORE_OPTION_KEYS for k in keys)
            or not all(is_finite_number(p) and 0 <= p <= 1 for p in values)
            or abs(sum(values) - 1) > PROBABILITY_SUM_TOLERANCE):
        return None
    mean = 0
    for key in SCORE_OPTION_KEYS:
        mean = mean + int(key) * probabilities[key]
    if abs(mean - score) > SCORE_MEAN_TOLERANCE:
        return None
    return {"score": score, "confidence": confidence, "probabilities": probabilities}


# ── Patch-review half shared by jev_review and jev_gate (index.js) ────────────
RUBRICS = ["correctness", "spec_match", "test_gap", "blast_radius"]


def project_review_half(answers, auto_accept, review_at, composite_floor, truncated):
    scores = {}
    valid = {}
    invalid = False
    for key in RUBRICS:
        parsed = validate_score_answer(answers.get(key))
        if parsed is None:
            scores[key] = {"score": None, "confidence": None, "probabilities": None, "status": "invalid_response"}
            invalid = True
        else:
            scores[key] = parsed
            valid[key] = parsed
    safe_to_apply = validate_noul_answer(answers.get("safe_to_apply"))
    if safe_to_apply is None:
        invalid = True
    base = {
        "safe_to_apply": safe_to_apply,
        "scores": scores,
        "weights": dict(REVIEW_WEIGHTS),
        "thresholds": {"auto_accept": auto_accept, "review_at": review_at, "composite_floor": composite_floor},
    }
    if invalid:
        return {**base, "action": "escalate", "status": "invalid_response", "composite": None,
                "reason_codes": ["invalid_response"], "limiting_rubrics": []}
    # Per-rubric favorability: each rubric's 0..1 contribution to the
    # composite, used to name the limiting rubric(s) when the floor blocks auto.
    favorability = {
        "correctness": valid["correctness"]["score"] / 2,
        "spec_match": valid["spec_match"]["score"] / 2,
        "test_gap": 1 - valid["test_gap"]["score"] / 2,
        "blast_radius": 1 - valid["blast_radius"]["score"] / 2,
    }
    min_favorability = min(favorability[r] for r in RUBRICS)
    composite = review_composite(valid["correctness"]["score"], valid["spec_match"]["score"],
                                 valid["test_gap"]["score"], valid["blast_radius"]["score"])
    # Unknown confidence on any rubric is unknown overall, never a number
    # that could satisfy a threshold.
    confidences = [valid[r]["confidence"] for r in RUBRICS]
    min_confidence = None if any(c is None for c in confidences) else min(confidences)
    raw_action = review_action(composite, safe_to_apply, min_confidence, auto_accept, review_at, composite_floor)
    action = require_complete_context(raw_action, truncated)
    reason_codes = []
    limiting = []
    if min_confidence is None:
        reason_codes.append("unknown_confidence")
        limiting = [r for r in RUBRICS if valid[r]["confidence"] is None]
    elif min_confidence < review_at:
        reason_codes.append("confidence_below_review")
        limiting = [r for r in RUBRICS if valid[r]["confidence"] == min_confidence]
    if safe_to_apply < review_at:
        reason_codes.append("safe_to_apply_below_review")
    if raw_action != "escalate":
        if safe_to_apply < auto_accept:
            reason_codes.append("safe_to_apply_below_auto_accept")
        if min_confidence is not None and min_confidence < auto_accept:
            reason_codes.append("confidence_below_auto_accept")
            if not limiting:
                limiting = [r for r in RUBRICS if valid[r]["confidence"] == min_confidence]
        if composite < composite_floor:
            reason_codes.append("composite_below_floor")
            # Tolerant equality: complementary scores tie up to a float ulp.
            if not limiting:
                limiting = [r for r in RUBRICS if abs(favorability[r] - min_favorability) <= 1e-12]
    if truncated:
        reason_codes.append("incomplete_context")
    if action == "auto":
        reason_codes.append("accepted")
    return {**base, "action": action, "composite": composite, "reason_codes": reason_codes, "limiting_rubrics": limiting}
