"""Extract only public, allowlisted stock-search terms from draft text."""

import re

TOPICS = {
    "طبیعت": "nature",
    "جنگل": "forest",
    "کوه": "mountain",
    "دریا": "ocean",
    "کتاب": "book",
    "آموزش": "education",
    "پزشکی": "healthcare",
    "تکنولوژی": "technology",
    "گل": "flower",
    "باران": "rain",
    "آسمان": "sky",
    "غذا": "food",
    "گربه": "cat",
    "ورزش": "sports",
    "معماری": "architecture",
    "هنر": "art",
    "قهوه": "coffee",
}
PUBLIC_TERMS = set(TOPICS.values()) | {"beach", "city", "sunset", "river", "dog", "car", "winter"}
PRIVATE = re.compile(r"https?://\S+|\S+@\S+|@\w+|\+?\d[\d .()-]{4,}\d", re.I)


def suggest_query(text):
    words = re.findall(r"[^\W\d_]+", PRIVATE.sub(" ", text).casefold())
    topics = [TOPICS.get(word, word) for word in words]
    return " ".join(dict.fromkeys(word for word in topics if word in PUBLIC_TERMS)) or "abstract"


_SMALL = (
    "صفر یک دو سه چهار پنج شش هفت هشت نه ده یازده دوازده سیزده چهارده پانزده شانزده هفده هجده نوزده"
).split()
_TENS = "صفر ده بیست سی چهل پنجاه شصت هفتاد هشتاد نود".split()
_HUNDREDS = "صفر صد دویست سیصد چهارصد پانصد ششصد هفتصد هشتصد نهصد".split()


def _number(number):
    if number < 20:
        return _SMALL[number]
    for unit, label in ((1_000_000, "میلیون"), (1000, "هزار"), (100, None), (10, None)):
        if number >= unit:
            whole, remainder = divmod(number, unit)
            if unit == 100:
                prefix = _HUNDREDS[whole]
            elif unit == 10:
                prefix = _TENS[whole]
            else:
                prefix = f"{_number(whole)} {label}"
            return prefix + (" و " + _number(remainder) if remainder else "")
    raise ValueError("INVALID_NUMBER")


def pronunciation(text):
    def replace(match):
        digits = match.group(0)
        if len(digits) > 9 or (len(digits) > 1 and int(digits[0]) == 0):
            return " ".join(_SMALL[int(digit)] for digit in digits)
        return _number(int(digits))

    return re.sub(r"\d+", replace, text)
