import re
import unicodedata
from dataclasses import dataclass, replace
from html import escape
from urllib.parse import urlsplit


class InvalidUtf16SpanBoundaryError(ValueError):
    """A span cannot be represented in the canonical UTF-16 text."""


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def utf16_boundaries(text: str) -> dict[int, int]:
    offsets, units = {0: 0}, 0
    for index, char in enumerate(text, 1):
        units += 2 if ord(char) > 0xFFFF else 1
        offsets[units] = index
    return offsets


KINDS = {
    "BOLD": "bold", "ITALIC": "italic", "UNDERLINE": "underline",
    "STRIKE": "strikethrough", "SPOILER": "spoiler", "CODE": "code",
    "PRE": "pre", "LINK": "text_link", "QUOTE": "blockquote", "MENTION": "mention",
    "HASHTAG": "hashtag",
}


def safe_link(url: str) -> bool:
    if not url or any(ord(char) < 33 or ord(char) == 127 for char in url) or "\\" in url:
        return False
    try:
        split = urlsplit(url)
        return split.scheme in {"http", "https", "tg"} and bool(split.hostname)
    except ValueError:
        return False


@dataclass(frozen=True)
class FormatSpanUtf16:
    offset: int
    length: int
    kind: str
    url: str | None = None


@dataclass(frozen=True)
class CanonicalRichText:
    plain_text: str
    spans_utf16: tuple[FormatSpanUtf16, ...] = ()

    def __post_init__(self):
        if len(self.plain_text) > 32768:
            raise ValueError("Canonical text exceeds 32768 characters")
        normalized = unicodedata.normalize("NFC", self.plain_text)
        spans = tuple(self.spans_utf16)
        if spans:
            bounds = utf16_boundaries(self.plain_text)
            edges = {0, max(bounds)}
            for span in spans:
                if (
                    span.offset < 0 or span.length < 1
                    or span.offset not in bounds or span.offset + span.length not in bounds
                ):
                    raise InvalidUtf16SpanBoundaryError("Invalid UTF-16 span boundaries")
                if span.kind not in KINDS or (
                    span.kind == "LINK" and not safe_link(span.url or "")
                ):
                    raise ValueError("Unsupported span kind or link")
                if span.kind != "LINK" and span.url is not None:
                    raise ValueError("Only LINK spans may have a URL")
                edges.update((span.offset, span.offset + span.length))
            if normalized != self.plain_text:
                positions = sorted(edges)
                parts = [
                    unicodedata.normalize("NFC", self.plain_text[bounds[a]:bounds[b]])
                    for a, b in zip(positions[:-1], positions[1:], strict=True)
                ]
                if "".join(parts) != normalized:
                    raise InvalidUtf16SpanBoundaryError("Span splits an NFC normalization sequence")
                remap = {0: 0}
                for a, b, part in zip(positions[:-1], positions[1:], parts, strict=True):
                    remap[b] = remap[a] + utf16_len(part)
                spans = tuple(
                    replace(s, offset=remap[s.offset],
                            length=remap[s.offset + s.length] - remap[s.offset])
                    for s in spans
                )
        object.__setattr__(self, "plain_text", normalized)
        object.__setattr__(self, "spans_utf16", spans)


_PLACEHOLDER = re.compile(r"\{(body|title|brand|destination|hashtags|source_credit)\}")
_TEMPLATE = re.compile(
    r"(?:[^{}]|\{(?:title|brand|destination|hashtags|source_credit)\})*\{body\}"
    r"(?:[^{}]|\{(?:title|brand|destination|hashtags|source_credit)\})*"
)


@dataclass(frozen=True)
class SafePlaceholderTemplate:
    template: str

    def __post_init__(self):
        if not _TEMPLATE.fullmatch(self.template):
            raise ValueError("Invalid template")

    def render(self, body: CanonicalRichText, *, bold_first_line=False, **values):
        prefix, suffix = self.template.split("{body}")

        def substitute(match):
            return str(values.get(match[1], ""))

        head, tail = _PLACEHOLDER.sub(substitute, prefix), _PLACEHOLDER.sub(substitute, suffix)
        spans = [replace(span, offset=span.offset + utf16_len(head)) for span in body.spans_utf16]
        text = head + body.plain_text + tail
        if bold_first_line:
            offset = 0
            for line in text.splitlines(keepends=True):
                if line.strip():
                    spans.append(FormatSpanUtf16(offset, utf16_len(line.rstrip("\r\n")), "BOLD"))
                    break
                offset += utf16_len(line)
        return CanonicalRichText(text, tuple(spans))


@dataclass(frozen=True)
class DestinationCapabilityProfile:
    native_albums: bool
    supported_spans: frozenset[str]
    max_text_length: int = 4096
    max_caption_length: int = 1024
    max_metadata_parts: int | None = None


_BALE_TAGS = {"BOLD": "b", "ITALIC": "i", "CODE": "code", "PRE": "pre", "LINK": "a"}
_RUBIKA = {
    "BOLD": "Bold", "ITALIC": "Italic", "UNDERLINE": "Underline",
    "STRIKE": "Strike", "SPOILER": "Spoiler", "CODE": "Mono", "PRE": "Pre", "LINK": "Link",
}
PROFILES = {
    "TELEGRAM": DestinationCapabilityProfile(True, frozenset(KINDS)),
    "BALE": DestinationCapabilityProfile(True, frozenset(_BALE_TAGS)),
    "EITAA": DestinationCapabilityProfile(False, frozenset()),
    "RUBIKA_EXPERIMENTAL": DestinationCapabilityProfile(False, frozenset(_RUBIKA),
                                                       max_metadata_parts=30),
    "SOROUSH_EXPERIMENTAL": DestinationCapabilityProfile(False, frozenset()),
}


@dataclass(frozen=True)
class CompilationResult:
    payload: dict
    degradations: list[str]
    length_overflow: bool
    split_preview: tuple[str, ...]
    album_action: str | None = None


def _split_preview(text, limit):
    pending = text
    while pending:
        units, last_line = 0, 0
        for index, char in enumerate(pending):
            units += 2 if ord(char) > 0xFFFF else 1
            if units > limit:
                cut = last_line or index
                yield pending[:cut]
                pending = pending[cut:]
                break
            if char == "\n":
                last_line = index + 1
        else:
            yield pending
            return


def _expand_links(text, spans, bounds):
    insertions = {}
    for span in spans:
        if span.kind == "LINK":
            insertions.setdefault(bounds[span.offset + span.length], []).append(f" ({span.url})")
    output, start = [], 0
    for end, links in sorted(insertions.items()):
        output.extend((text[start:end], *dict.fromkeys(links)))
        start = end
    return "".join((*output, text[start:]))


def _bale_html(text, spans, bounds):
    events = {}
    for index, span in enumerate(spans):
        start, end = bounds[span.offset], bounds[span.offset + span.length]
        events.setdefault(start, []).append((index, span))
        events.setdefault(end, []).append((index, None))
    active, opened, output, previous = {}, [], [], 0
    for edge in sorted({*events, len(text)}):
        output.append(escape(text[previous:edge]).replace("*", "&#42;"))
        for index, span in events.get(edge, ()):
            if span is None:
                del active[index]
            else:
                active[index] = span
        desired = sorted(active)
        first_link = next((i for i in desired if active[i].kind == "LINK"), None)
        desired = [i for i in desired if active[i].kind != "LINK" or i == first_link]
        keep = 0
        for (index, _), next_index in zip(opened, desired, strict=False):
            if index != next_index:
                break
            keep += 1
        output.extend(f"</{tag}>" for _, tag in reversed(opened[keep:]))
        del opened[keep:]
        for index in desired[keep:]:
            span = active[index]
            tag = _BALE_TAGS[span.kind]
            attr = f' href="{escape(span.url)}"' if span.kind == "LINK" else ""
            output.append(f"<{tag}{attr}>")
            opened.append((index, tag))
        previous = edge
    return "".join(output)


def compile_rich_text(content, platform, *, caption=False, album_count=1,
                      album_policy="CANCEL_FOR_INCOMPATIBLE_DEST"):
    if album_policy not in {"SEQUENTIAL_POSTS", "CANCEL_FOR_INCOMPATIBLE_DEST"}:
        raise ValueError("Invalid album policy")
    profile = PROFILES[platform]
    text = content.plain_text
    spans = sorted(content.spans_utf16, key=lambda s: (s.offset, -s.length, s.kind, s.url or ""))
    supported = [s for s in spans if s.kind in profile.supported_spans]
    unsupported = [s for s in spans if s.kind not in profile.supported_spans]
    degradations = sorted({f"UNSUPPORTED_{s.kind}" for s in unsupported})
    action = None
    if album_count > 1 and not profile.native_albums:
        action = album_policy
        degradations.append("NATIVE_ALBUM_UNAVAILABLE")
    if platform.endswith("_EXPERIMENTAL"):
        degradations.append("EXPERIMENTAL")
    if platform == "RUBIKA_EXPERIMENTAL" and len(supported) > profile.max_metadata_parts:
        unsupported.extend(supported)
        supported = []
        degradations.append("METADATA_LIMIT_EXCEEDED")
    bounds = utf16_boundaries(text) if spans else {0: 0}
    plain = _expand_links(text, unsupported, bounds)
    payload = {"plain_text": plain}
    if platform == "TELEGRAM":
        payload["entities"] = [
            {"offset": s.offset, "length": s.length, "type": KINDS[s.kind],
             **({"url": s.url} if s.url else {})} for s in supported
        ]
    elif platform == "BALE":
        payload["html"] = _bale_html(text, supported, bounds)
    elif platform == "RUBIKA_EXPERIMENTAL":
        payload["meta_data_parts"] = [
            {"from_index": s.offset, "length": s.length, "type": _RUBIKA[s.kind],
             **({"link": {"type": "hyperlink", "hyperlink_data": {"url": s.url}}} if s.url else {})}
            for s in supported
        ]
    limit = profile.max_caption_length if caption else profile.max_text_length
    overflow = utf16_len(plain) > limit
    return CompilationResult(payload, degradations, overflow,
                             tuple(_split_preview(plain, limit)) if overflow else (), action)
