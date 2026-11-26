"""Contextual Arabic glyph shaping for Pillow's BASIC layout engine."""

import unicodedata

DIGITS = str.maketrans("0123456789٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹۰۱۲۳۴۵۶۷۸۹")


def _forms():
    forms = {}
    for code in range(0xFB50, 0xFF00):
        char = chr(code)
        decomposition = unicodedata.decomposition(char).split()
        if len(decomposition) == 2 and decomposition[0] in {
            "<isolated>",
            "<initial>",
            "<medial>",
            "<final>",
        }:
            forms.setdefault(chr(int(decomposition[1], 16)), {})[decomposition[0]] = char
    return forms


FORMS = _forms()
MIRRORED = str.maketrans("()[]{}", ")(][}{")


def visual_text(text):
    text = text.translate(DIGITS)
    clusters = []
    for char in text:
        if clusters and unicodedata.category(char) == "Mn":
            clusters[-1] += char
        else:
            clusters.append(char)
    bases = [cluster[0] for cluster in clusters]
    shaped = []
    direction = "R"
    for index, cluster in enumerate(clusters):
        char = cluster[0]
        forms = FORMS.get(char, {})
        previous = FORMS.get(bases[index - 1], {}) if index else {}
        following = FORMS.get(bases[index + 1], {}) if index + 1 < len(bases) else {}
        joins_previous = "<initial>" in previous and "<final>" in forms
        joins_following = "<initial>" in forms and "<final>" in following
        form = {
            (True, True): "<medial>",
            (True, False): "<final>",
            (False, True): "<initial>",
            (False, False): "<isolated>",
        }[joins_previous, joins_following]
        bidi = unicodedata.bidirectional(char)
        if bidi in {"AL", "R", "L", "EN", "AN"}:
            direction = "R" if bidi in {"AL", "R"} else "L"
        elif bidi not in {"ES", "CS", "ET"}:
            direction = "R"
        if char not in {"\u200c", "\u200d"}:
            shaped.append((forms.get(form, char) + cluster[1:], direction))
    runs = []
    for cluster, direction in shaped:
        if not runs or runs[-1][0] != direction:
            runs.append((direction, []))
        runs[-1][1].append(cluster)
    return "".join(
        "".join(reversed(chars)).translate(MIRRORED) if direction == "R" else "".join(chars)
        for direction, chars in reversed(runs)
    )
