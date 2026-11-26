"""Bounded, deterministic rules and a search-only Thompson NFA."""

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from relayforge.core.rich_text import (
    CanonicalRichText,
    FormatSpanUtf16,
    SafePlaceholderTemplate,
    utf16_boundaries,
    utf16_len,
)

MAX_FILTER_DEPTH = 12
MAX_FILTER_NODES = 64
CONTENT_KINDS = {"TEXT", "IMAGE", "VIDEO", "AUDIO", "VOICE_NOTE", "DOCUMENT", "ALBUM"}


class RuleError(ValueError):
    pass


class ThompsonNFA:
    def __init__(self, pattern, *, ignore_case=False):
        if not isinstance(pattern, str) or len(pattern) > 256:
            raise RuleError("INVALID_PATTERN_LENGTH")
        self.pattern, self.index, self.states = pattern, 0, []
        self.fold = str.lower if ignore_case else lambda char: char
        self.start, end = self._expression()
        if self.index != len(pattern):
            raise RuleError("INVALID_PATTERN")
        self.states[end][0] = "match"

    def _state(self, kind="epsilon", value=None, *edges):
        if len(self.states) == 512:
            raise RuleError("PATTERN_STATE_LIMIT")
        self.states.append([kind, value, list(edges)])
        return len(self.states) - 1

    def _peek(self):
        return self.pattern[self.index:self.index + 1]

    def _take(self):
        char = self._peek()
        if not char:
            raise RuleError("INVALID_PATTERN")
        self.index += 1
        return char

    def _expression(self):
        start, end = self._sequence()
        while self._peek() == "|":
            self._take()
            other, other_end = self._sequence()
            start = self._state("epsilon", None, start, other)
            self.states[other_end][2].append(end)
        return start, end

    def _sequence(self):
        start = end = self._state()
        while self._peek() not in {"", ")", "|"}:
            atom, tail = self._atom()
            quantifier = self._peek()
            if quantifier in {"*", "+", "?"}:
                self._take()
                exit_state = self._state()
                if quantifier != "+":
                    atom = self._state("epsilon", None, atom, exit_state)
                if quantifier != "?":
                    self.states[tail][2].append(atom)
                self.states[tail][2].append(exit_state)
                tail = exit_state
            self.states[end][2].append(atom)
            end = tail
        return start, end

    def _escaped(self):
        char = self._take()
        if char.isalnum():
            raise RuleError("UNSUPPORTED_PATTERN_ESCAPE")
        return char

    def _atom(self):
        char = self._take()
        if char == "(":
            result = self._expression()
            if self._take() != ")":
                raise RuleError("INVALID_PATTERN")
            return result
        if char in "*+?{}":
            raise RuleError("INVALID_PATTERN")
        end = self._state()
        if char == "[":
            negate = self._peek() == "^"
            if negate:
                self._take()
            ranges = []
            while self._peek() != "]":
                lower = self._class_char()
                if self._peek() == "-" and self.pattern[self.index + 1:self.index + 2] != "]":
                    self._take()
                    upper = self._class_char()
                else:
                    upper = lower
                if lower > upper:
                    raise RuleError("INVALID_PATTERN_RANGE")
                ranges.append((lower, upper))
            self._take()
            if not ranges:
                raise RuleError("INVALID_PATTERN_CLASS")
            return self._state("class", (ranges, negate), end), end
        if char in {".", "^", "$"}:
            return self._state(char, None, end), end
        literal = self._escaped() if char == "\\" else char
        return self._state("literal", self.fold(literal), end), end

    def _class_char(self):
        char = self._take()
        return self.fold(self._escaped() if char == "\\" else char)

    def search(self, text, *, anchored=False):
        active = {self.start}
        for position in range(len(text) + 1):
            stack, seen, consuming = list(active), set(), []
            if not anchored:
                stack.append(self.start)
            while stack:
                state = stack.pop()
                if state in seen:
                    continue
                seen.add(state)
                kind, value, edges = self.states[state]
                if kind == "match" and (not anchored or position == len(text)):
                    return True
                if kind == "epsilon" or (kind == "^" and position == 0) or (
                    kind == "$" and position == len(text)
                ):
                    stack.extend(edges)
                elif kind in {".", "literal", "class"}:
                    consuming.append((kind, value, edges[0]))
            if position < len(text):
                char = self.fold(text[position])
                active = {
                    target for kind, value, target in consuming
                    if (kind == "literal" and char == value)
                    or (kind == "." and char != "\n")
                    or (kind == "class"
                        and any(a <= char <= b for a, b in value[0]) != value[1])
                }
        return False


@dataclass(frozen=True)
class FilterContext:
    text: CanonicalRichText
    content_kind: str = "TEXT"
    source_id: str | None = None
    access_class: str = "PROCESSABLE"
    platform: str = "TELEGRAM"
    transformed: bool = False


def compile_filter(tree):
    count = 0

    def compile_node(node, depth):
        nonlocal count
        count += 1
        if depth > MAX_FILTER_DEPTH or count > MAX_FILTER_NODES or not isinstance(node, dict):
            raise RuleError("INVALID_FILTER_COMPLEXITY")
        op = node.get("op")
        if op in ("ALL", "ANY", "NOT"):
            items = node.get("children")
            if not isinstance(items, list) or not items or (op == "NOT" and len(items) != 1):
                raise RuleError("INVALID_FILTER_CHILDREN")
            children = tuple(compile_node(child, depth + 1) for child in items)
            if op == "NOT":
                return lambda ctx: not children[0](ctx)
            aggregate = all if op == "ALL" else any
            return lambda ctx: aggregate(child(ctx) for child in children)
        value = node.get("value")
        if op == "regex_match":
            flag = node.get("ignore_case", False)
            if type(flag) is not bool:
                raise RuleError("INVALID_FILTER_VALUE")
            matcher = ThompsonNFA(value, ignore_case=flag)
            return lambda ctx: matcher.search(ctx.text.plain_text)
        if op in ("keyword_any", "source_id_in", "content_kind_in"):
            if not isinstance(value, list) or not value or len(value) > 64 or any(
                not isinstance(item, str) or not 0 < len(item) <= 256 for item in value
            ):
                raise RuleError("INVALID_FILTER_VALUE")
            if op == "keyword_any":
                needles = tuple(item.casefold() for item in value)

                def keywords(ctx):
                    text = ctx.text.plain_text.casefold()
                    return any(needle in text for needle in needles)

                return keywords
            if op == "content_kind_in" and not set(value) <= CONTENT_KINDS:
                raise RuleError("INVALID_CONTENT_KIND")
            choices = frozenset(value)
            return lambda ctx: (ctx.source_id if op == "source_id_in" else
                                ctx.content_kind) in choices
        if op in ("min_utf16_len", "max_utf16_len"):
            if type(value) is not int or not 0 <= value <= 65536:
                raise RuleError("INVALID_FILTER_VALUE")
            if op == "min_utf16_len":
                return lambda ctx: utf16_len(ctx.text.plain_text) >= value
            return lambda ctx: utf16_len(ctx.text.plain_text) <= value
        if op in ("has_link", "has_hashtag", "has_mention"):
            kind = op[4:].upper()
            pattern = {"LINK": r"(?:https?://|tg://)\S+", "HASHTAG": r"(?<!\w)#\w+",
                       "MENTION": r"(?<!\w)@\w+"}[kind]
            return lambda ctx: any(span.kind == kind for span in ctx.text.spans_utf16) or bool(
                re.search(pattern, ctx.text.plain_text)
            )
        raise RuleError("INVALID_FILTER_OPERATOR")

    if tree == {}:
        return lambda ctx: True
    return compile_node(tree, 1)


def evaluate_filter(compiled, context):
    if context.access_class == "PROTECTED_NO_FORWARD":
        return False, "PROTECTED_NO_FORWARD"
    if context.access_class == "REFERENCE_ONLY" and (
        context.platform != "TELEGRAM" or context.transformed
    ):
        return False, "REFERENCE_ONLY_INCOMPATIBLE"
    matched = compiled(context)
    return matched, "FILTER_MATCH" if matched else "FILTER_NO_MATCH"


def resolve_policy(workspace_default, route_policy, destination_override):
    return workspace_default | route_policy | destination_override


def _rewrite(content, edits):
    """Keep surviving portions of spans and exclude newly inserted replacement text."""
    text = content.plain_text
    bounds = utf16_boundaries(text)
    # Character boundaries to UTF-16 offsets are already ordered.
    offsets = list(bounds)
    segments, output, start, output_units = [], [], 0, 0
    for a, b, replacement in [*edits, (len(text), len(text), "")]:
        output.extend((text[start:a], replacement))
        if start < a:
            segments.append((offsets[start], offsets[a], output_units))
        output_units += offsets[a] - offsets[start] + utf16_len(replacement)
        start = b
    spans = []
    for span in content.spans_utf16:
        for a, b, cursor in segments:
            left, right = max(a, span.offset), min(b, span.offset + span.length)
            if left < right:
                if len(spans) >= 4096:
                    raise RuleError("TRANSFORM_SPAN_LIMIT")
                spans.append(FormatSpanUtf16(cursor + left - a, right - left, span.kind, span.url))
    return CanonicalRichText("".join(output), tuple(spans))


def transform(content, policy):
    pairs = policy.get("replacements", {})
    if not isinstance(pairs, dict) or len(pairs) > 64 or any(
        not key or not isinstance(value, str) for key, value in pairs.items()
    ):
        raise RuleError("INVALID_REPLACEMENTS")
    stripped = policy.get("strip_phrases", [])
    allowed = policy.get("allowed_mentions", [])
    if any(not isinstance(items, list) or len(items) > 64 or any(
        not isinstance(item, str) or not item for item in items
    ) for items in (stripped, allowed)):
        raise RuleError("INVALID_TRANSFORM_LIST")
    pairs = pairs | dict.fromkeys(stripped, "")
    if pairs:
        pattern = re.compile("|".join(map(re.escape, sorted(pairs, key=lambda s: (-len(s), s)))))
        content = _rewrite(content, [
            (match.start(), match.end(), pairs[match[0]])
            for match in pattern.finditer(content.plain_text)
        ])
    if policy.get("strip_third_party_mentions"):
        authorized = {mention.casefold().lstrip("@") for mention in allowed}
        content = _rewrite(content, [
            (match.start(), match.end(), "") for match in
            re.finditer(r"(?<!\w)@(\w+)", content.plain_text)
            if match[1].casefold() not in authorized
        ])
    template = policy.get("template", "{body}")
    rendered = SafePlaceholderTemplate(template).render(
        content, bold_first_line=policy.get("bold_first_line", False),
        **{key: policy.get(key, "") for key in ("title", "brand", "destination", "hashtags",
                                                "source_credit")},
    )
    pre, post = policy.get("prefix", ""), policy.get("suffix", "")
    spans = tuple(FormatSpanUtf16(s.offset + utf16_len(pre), s.length, s.kind, s.url)
                  for s in rendered.spans_utf16)
    return CanonicalRichText(pre + rendered.plain_text + post, spans)


@dataclass(frozen=True)
class BrandSpec:
    signature_template: str
    placement_policy: str = "FOOTER"
    separator_line: str = "━━━━━━━━━━━━━━"


def inject_brand_identity(content, brand):
    if not brand or brand.placement_policy == "NONE" or not brand.signature_template.strip():
        return content
    signature = unicodedata.normalize("NFC", brand.signature_template.strip())
    lines = content.plain_text.strip().splitlines()
    target = signature.splitlines()
    present = any(lines[i:i + len(target)] == target for i in range(len(lines)))
    if present:
        return content
    separator = "\n" + brand.separator_line + "\n"
    head = signature + separator if brand.placement_policy in {"HEADER", "BOTH"} else ""
    tail = separator + signature if brand.placement_policy in {"FOOTER", "BOTH"} else ""
    if not content.plain_text:
        return CanonicalRichText(signature)
    return transform(content, {"prefix": head, "suffix": tail})


def fingerprint(text):
    normalized = " ".join(unicodedata.normalize("NFC", text).casefold().split())
    exact = hashlib.sha256(normalized.encode()).hexdigest()
    scores = [0] * 64
    shingles = {normalized[i:i + 3] for i in range(max(1, len(normalized) - 2))}
    for shingle in sorted(shingles):
        digest = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest())
        for bit in range(64):
            scores[bit] += 1 if digest & (1 << bit) else -1
    value = sum(1 << bit for bit, score in enumerate(scores) if score > 0)
    return exact, value if value < 2**63 else value - 2**64


def duplicate_decision(candidate, existing):
    exact, near = candidate
    mask = 2**64 - 1
    hold = False
    for old_exact, old_near in existing:
        if exact == old_exact:
            return "DUPLICATE_EXACT_SKIP"
        if old_near is not None:
            distance = ((near ^ old_near) & mask).bit_count()
            hold |= 0 < distance <= 6
    return "AMBIGUOUS_NEAR_DUPLICATE_HOLD_REVIEW" if hold else "UNIQUE_PASS"


def check_route_dag(edges):
    graph = {}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
    visiting, visited = set(), set()
    for root in graph:
        stack = [(root, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                visiting.remove(node)
                visited.add(node)
            elif node in visiting:
                raise RuleError("ROUTE_CYCLE_DETECTED")
            elif node not in visited:
                visiting.add(node)
                stack.append((node, True))
                stack.extend((child, False) for child in graph.get(node, ()))
