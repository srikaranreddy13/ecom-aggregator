"""
Domain-agnostic attribute extraction.

THE DESIGN QUESTION THIS ANSWERS. An aggregator has to work on anything a
retailer sells -- phones, laptops, shirts, shampoo, ceiling fans, books. The
obvious approach is a category detector plus per-category attribute rules
("phones have storage, appliances have wattage, clothes have sizes"). That
approach does not terminate: every new domain needs another branch, and until
someone writes it, that domain resolves badly and silently.

So there are no categories here. Instead, one universal observation:

    A QUANTITY WITH A UNIT IS A HARD SPECIFICATION, IN EVERY DOMAIN.

    iPhone 15 256GB          -> gb: 256        Mixer 750W        -> w: 750
    Kettle 1.5 L             -> l: 1.5         Shampoo 500ml     -> l: 0.5
    Watch 44mm               -> mm: 44         TV 32 inch        -> in: 32
    Shirt size 42            -> size: 42       Bulbs pack of 4   -> pack: 4

Two listings that disagree on a quantity they BOTH state are different products.
That rule needs no taxonomy, so a domain nobody anticipated still resolves
correctly on the day it first appears.

COMPARISON USES THE MAXIMUM PER UNIT, NOT THE FULL SET. This is the subtle part,
and it fixes a real bug. A laptop title carries two `gb` figures:

    "ThinkPad E14 16GB RAM 512GB SSD"   -> gb: {16, 512}

The old code scanned for the first plausible `\\d+ ?gb` and returned 16 -- the
RAM -- as the storage size, so two laptops differing only in disk size shared a
block key on the wrong attribute. Comparing maxima instead:

    128        vs 256           ->  128 != 256    different   (correct)
    {16, 512}  vs {16, 1024}    ->  512 != 1024   different   (correct)
    {128}      vs {6, 128}      ->  128 == 128    same        (correct: the
                                                   second title merely also
                                                   mentions RAM)

All three come out right, and NOTHING has to know which number means RAM. Set
equality would fail the third case; set intersection would fail the second.

WHAT IS DELIBERATELY NOT HERE. No brand list, no colour taxonomy beyond base
colours, no product-type keywords. Anything that would need maintenance per
domain is either derived from the source's own metadata (a retailer API tells
you its category -- do not guess it) or left out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- units
#
# Each entry maps a spelling to (canonical_dimension, multiplier). Values are
# converted into the canonical unit so "1TB" and "1024GB" compare equal and
# "500ml" and "0.5L" compare equal.
#
# Adding a unit here extends the system to a new domain without touching any
# other file, and without a category branch anywhere.

UNITS: dict[str, tuple[str, float]] = {
    # digital storage / memory -> GB
    "gb": ("gb", 1.0), "gigabyte": ("gb", 1.0), "gigabytes": ("gb", 1.0),
    "tb": ("gb", 1024.0), "terabyte": ("gb", 1024.0), "terabytes": ("gb", 1024.0),
    "mb": ("gb", 1 / 1024.0),

    # electrical power -> W
    "w": ("w", 1.0), "watt": ("w", 1.0), "watts": ("w", 1.0), "kw": ("w", 1000.0),

    # volume -> L
    "l": ("l", 1.0), "lt": ("l", 1.0), "ltr": ("l", 1.0),
    "litre": ("l", 1.0), "litres": ("l", 1.0), "liter": ("l", 1.0), "liters": ("l", 1.0),
    "ml": ("l", 0.001),

    # mass -> g
    "g": ("g", 1.0), "gm": ("g", 1.0), "gms": ("g", 1.0), "gram": ("g", 1.0),
    "grams": ("g", 1.0), "kg": ("g", 1000.0), "kgs": ("g", 1000.0),

    # length -> mm
    # Bare "m" is deliberately ABSENT. It is far too common in product titles as
    # something else -- size M, the M3 chip, model M240 -- and the cost is
    # asymmetric: a wrongly-extracted quantity blocks a correct merge, whereas a
    # missed one merely leaves the decision to the embeddings. Cable lengths in
    # metres go unextracted, which is the cheaper failure.
    "mm": ("mm", 1.0), "cm": ("mm", 10.0),

    # display / physical size -> in
    "inch": ("in", 1.0), "inches": ("in", 1.0), '"': ("in", 1.0), "''": ("in", 1.0),

    # battery -> mAh
    "mah": ("mah", 1.0), "wh": ("wh", 1.0),

    # misc consumer specs
    "mp": ("mp", 1.0), "megapixel": ("mp", 1.0), "megapixels": ("mp", 1.0),
    "hz": ("hz", 1.0), "khz": ("hz", 1000.0),
    "ton": ("ton", 1.0), "tons": ("ton", 1.0),   # air conditioners
    "seater": ("seater", 1.0),                    # furniture
    "ply": ("ply", 1.0),                          # tissue, tyres
    "rpm": ("rpm", 1.0),
    "star": ("star", 1.0),                        # BEE energy rating
}

# Sorted longest-first so "litres" wins over "l" and "inches" over "inch".
_UNIT_PATTERN = "|".join(
    re.escape(u) for u in sorted(UNITS, key=len, reverse=True)
)

# A number, an optional space OR HYPHEN, then a unit. The hyphen form matters:
# "13-inch" and "3-ply" are as common as "13 inch" in real listings, and without
# it a MacBook's screen size went unextracted while a competing listing's
# "13 inch" was found -- an asymmetry that is worse than missing both.
#
# The unit must end at a non-alphanumeric boundary, so "1 Sterling" is not 1 star.
_QUANTITY_RE = re.compile(
    rf"(\d+(?:\.\d+)?)[\s-]*({_UNIT_PATTERN})(?![a-z0-9])",
    re.I,
)

# Plausibility windows. Without these, "Made in 2024" yields a 2024-inch screen
# and a phone number becomes a wattage. A value outside its window is discarded
# rather than trusted, because a wrong hard attribute blocks a correct merge.
# Relative tolerance when comparing two values of the same unit.
#
# Most specs are exact: 128GB is never "about" 256GB, and a 750W mixer is not a
# 500W one. But some are MARKETING-ROUNDED, and treating those exactly invents
# conflicts between listings for one product:
#
#   * screen size -- Apple sells the "13-inch" MacBook Air whose panel is 13.6".
#     Retailers use both numbers for the same laptop. Exact matching split it
#     into two products. 6% keeps 13/13.6 together while 43 vs 55 stays apart.
#   * battery -- "typical" vs "minimum" capacity differ by a percent or two
#     (5000 vs 4990 mAh) for the same cell.
#
# Anything not listed compares exactly (a float-noise epsilon aside).
TOLERANCE: dict[str, float] = {
    "in": 0.06,
    "mah": 0.03,
}

_EXACT_EPSILON = 1e-6


PLAUSIBLE: dict[str, tuple[float, float]] = {
    "gb":     (0.5, 65536),
    "w":      (0.5, 20000),
    "l":      (0.005, 2000),
    "g":      (0.5, 200000),
    "mm":     (1, 20000),
    "in":     (0.5, 120),
    "mah":    (100, 100000),
    "wh":     (1, 5000),
    "mp":     (0.3, 500),
    "hz":     (1, 1000000),
    "ton":    (0.5, 20),
    "seater": (1, 20),
    "ply":    (1, 10),
    "rpm":    (10, 100000),
    "star":   (1, 5),
}

# ---------------------------------------------------------------- pack counts

_PACK_RES = [
    re.compile(r"\bpack\s+of\s+(\d{1,3})\b", re.I),
    re.compile(r"\bset\s+of\s+(\d{1,3})\b", re.I),
    re.compile(r"\b(\d{1,3})\s*[-\s]?pack\b", re.I),
    re.compile(r"\b(\d{1,3})\s+(?:jars?|pcs?|pieces?|refills?|units?|"
               r"count|combo|nos?)\b", re.I),
    re.compile(r"\bcombo\s+of\s+(\d{1,3})\b", re.I),
]

# ---------------------------------------------------------------- sizes
#
# Clothing and footwear are the one common domain where the hard spec is NOT a
# number with a unit. Handled explicitly because "M" and "L" are otherwise
# indistinguishable from ordinary words.

_LETTER_SIZES = {
    "xxxs": "xxxs", "xxs": "xxs", "xs": "xs", "s": "s", "m": "m", "l": "l",
    "xl": "xl", "xxl": "xxl", "2xl": "xxl", "xxxl": "xxxl", "3xl": "xxxl",
    "4xl": "xxxxl", "xxxxl": "xxxxl",
}

# Requires the word "size" so a bare "L" in a title is never read as a size.
_SIZE_LETTER_RE = re.compile(
    r"\bsize\s*[:\-]?\s*(xxxs|xxs|xs|s|m|l|xl|xxl|2xl|xxxl|3xl|4xl|xxxxl)\b", re.I)
_SIZE_NUM_RE = re.compile(r"\bsize\s*[:\-]?\s*(\d{1,3})\b", re.I)
_UK_US_RE = re.compile(r"\b(?:uk|us|eu)\s*(\d{1,2}(?:\.\d)?)\b", re.I)

# ---------------------------------------------------------------- colours

BASE_COLOURS = {
    "black", "white", "blue", "green", "red", "silver", "gold", "grey", "gray",
    "purple", "pink", "yellow", "orange", "beige", "brown", "maroon", "navy",
    "teal", "cream", "ivory", "transparent", "clear", "multicolour", "multicolor",
}

# Marketing modifiers must never outrank a base colour: "Onyx Black" and "Black"
# have to land in the same block. A modifier is only used when no base colour is
# present at all ("Graphite" alone still identifies a variant).
MODIFIER_COLOURS = {
    "titanium", "graphite", "midnight", "starlight", "onyx", "lavender", "mint",
    "charcoal", "obsidian", "pearl", "sierra", "phantom", "rose", "sky",
    "olive", "mustard", "coral", "burgundy", "khaki", "denim", "sand",
}

_COLOUR_ALIASES = {"gray": "grey", "multicolor": "multicolour", "clear": "transparent"}

# ---------------------------------------------------------------- model codes

# Alphanumeric runs containing BOTH a letter and a digit: wh1000xm5, hl7756,
# a2643, gc1905, sm-s921b.
#
# The lookaheads matter. A naive `[a-z]+\d+[a-z]{0,3}` silently fails on a
# trailing digit ("wh1000xm5" -> "wh1000xm"), which merged Sony's XM4 with XM5.
# Do not "simplify" this pattern.
_MODEL_RE = re.compile(r"\b(?=[a-z0-9]*[a-z])(?=[a-z0-9]*\d)([a-z0-9]{3,14})\b", re.I)

_ORDINAL_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)\b(?:\s*(?:gen|generation))?|"
    r"\b(?:gen|generation)\s*(\d{1,2})\b", re.I)

# Words that look like model codes but are units or noise once a digit is next
# to them. Excluded so "128gb" is never also reported as model "128gb".
_MODEL_STOP_RE = re.compile(
    rf"^\d+(?:{_UNIT_PATTERN})$|^\d+$|^(?:5g|4g|3g|2g|wifi|usb|usbc|type[c]?)$",
    re.I)

NOISE_TOKENS = {
    "smartphone", "mobile", "phone", "5g", "4g", "dual", "sim", "unlocked",
    "with", "and", "the", "new", "latest", "official", "genuine", "sealed",
    "brand", "free", "shipping", "wireless", "bluetooth", "for", "in", "of",
    "combo", "offer", "sale", "best", "top", "premium", "original", "pack",
}


# ================================================================ extraction


@dataclass(frozen=True)
class Attributes:
    """Everything extracted from one title.

    `quantities` maps a canonical unit to the MAXIMUM value seen for it. See the
    module docstring for why the maximum rather than the set.
    """

    quantities: dict[str, float]
    colour: str | None
    model_number: str | None
    generation: str | None
    pack_count: int | None
    size: str | None

    def conflicts_with(self, other: "Attributes") -> str | None:
        """Return the attribute that proves these are different products, or None.

        Silent when either side lacks the attribute: a title that omits RAM is
        not evidence of a different product. Only a stated DISAGREEMENT counts.
        """
        for unit, value in self.quantities.items():
            theirs = other.quantities.get(unit)
            if theirs is None:
                continue
            tolerance = TOLERANCE.get(unit, _EXACT_EPSILON)
            if abs(value - theirs) > max(value, theirs) * tolerance:
                return f"{unit} ({value:g} vs {theirs:g})"

        for field in ("model_number", "generation", "size"):
            mine, theirs = getattr(self, field), getattr(other, field)
            if mine and theirs and mine != theirs:
                return f"{field} ({mine} vs {theirs})"

        if self.pack_count and other.pack_count and self.pack_count != other.pack_count:
            return f"pack_count ({self.pack_count} vs {other.pack_count})"

        return None

    def as_dict(self) -> dict:
        """Serialisable form, for the API and the frontend."""
        return {
            "quantities": {u: round(v, 4) for u, v in sorted(self.quantities.items())},
            "colour": self.colour,
            "model_number": self.model_number,
            "generation": self.generation,
            "pack_count": self.pack_count,
            "size": self.size,
        }

    def summary(self) -> str:
        """Short human-readable spec, e.g. '256gb · black · wh1000xm5'."""
        bits = [f"{v:g}{u}" for u, v in sorted(self.quantities.items())]
        if self.size:
            bits.append(f"size {self.size}")
        if self.pack_count:
            bits.append(f"x{self.pack_count}")
        if self.colour:
            bits.append(self.colour)
        if self.model_number:
            bits.append(self.model_number)
        if self.generation:
            bits.append(f"gen {self.generation}")
        return " · ".join(bits) or "no specs detected"


def extract_quantities(title: str) -> dict[str, float]:
    """Every number-with-unit in the title, normalised, maximum per unit."""
    found: dict[str, float] = {}
    for m in _QUANTITY_RE.finditer(title):
        raw = float(m.group(1))
        dimension, multiplier = UNITS[m.group(2).lower()]
        value = raw * multiplier

        lo, hi = PLAUSIBLE.get(dimension, (float("-inf"), float("inf")))
        if not (lo <= value <= hi):
            continue

        if dimension not in found or value > found[dimension]:
            found[dimension] = value
    return found


def extract_colour(title: str) -> str | None:
    """Base colour if present, otherwise a marketing modifier."""
    modifier = None
    for token in re.findall(r"[a-z]+", title.lower()):
        if token in BASE_COLOURS:
            return _COLOUR_ALIASES.get(token, token)
        if token in MODIFIER_COLOURS and modifier is None:
            modifier = token
    return modifier


def extract_generation(title: str) -> str | None:
    """Product generation from '2nd Gen', '(3rd Generation)' or 'Gen 5'.

    Kept separate from the model number: '2nd' is an ordinal, not a model code.
    Generation still discriminates products -- AirPods Pro 2nd and 3rd gen are
    different items -- so it is its own must-not-link attribute.
    """
    m = _ORDINAL_RE.search(title)
    if not m:
        return None
    return m.group(1) or m.group(2)


def extract_pack_count(title: str) -> int | None:
    for rx in _PACK_RES:
        m = rx.search(title)
        if m:
            value = int(m.group(1))
            if 1 <= value <= 200:
                return value
    return None


def extract_size(title: str) -> str | None:
    """Clothing/footwear size. Requires the word 'size' (or a UK/US/EU prefix)
    so that a stray 'L' or 'M' in a title is never mistaken for one."""
    m = _SIZE_LETTER_RE.search(title)
    if m:
        return _LETTER_SIZES[m.group(1).lower()]
    m = _SIZE_NUM_RE.search(title)
    if m:
        return m.group(1)
    m = _UK_US_RE.search(title)
    if m:
        return m.group(1)
    return None


def extract_model_number(title: str) -> str | None:
    """Longest alphanumeric token containing both letters and digits.

    Units, bare numbers and network generations are excluded -- each is handled
    by its own extractor and would otherwise be mislabelled as a model code.
    """
    cleaned = re.sub(r"[-_/]", "", title.lower())
    cleaned = _ORDINAL_RE.sub(" ", cleaned)
    cleaned = _QUANTITY_RE.sub(" ", cleaned)

    candidates = [
        m.group(1) for m in _MODEL_RE.finditer(cleaned)
        if not _MODEL_STOP_RE.match(m.group(1))
    ]
    return max(candidates, key=len) if candidates else None


def extract(title: str) -> Attributes:
    """Full attribute set for one title. The single entry point."""
    return Attributes(
        quantities=extract_quantities(title),
        colour=extract_colour(title),
        model_number=extract_model_number(title),
        generation=extract_generation(title),
        pack_count=extract_pack_count(title),
        size=extract_size(title),
    )


def normalise_title(title: str) -> str:
    """Strip marketing noise so the embedder sees the discriminative tokens."""
    t = title.lower()
    t = re.sub(r"[\(\)\[\],/|]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return " ".join(w for w in t.split() if w not in NOISE_TOKENS).strip()


# ================================================================ blocking


def block_key(brand: str | None, title: str, attrs: Attributes | None = None) -> tuple:
    """Coarse partition key. Listings with different keys are never compared.

    DELIBERATELY COARSE -- brand and colour only, no quantities.

    Blocking is a performance and safety mechanism, not the precision mechanism.
    Putting quantities in the key looks stronger but backfires: any listing whose
    title omits a spec gets its own singleton block, semantic clustering never
    runs on it, and recall collapses. Since these keys must be exact, every
    field added multiplies that risk.

    Precision comes from `Attributes.conflicts_with`, which is applied inside a
    block and stays silent when a spec is missing. That division -- coarse and
    exact for partitioning, fine and None-tolerant for vetoing -- is what makes
    the resolver work across domains it has never seen.
    """
    a = attrs or extract(title)
    return ((brand or "").strip().lower() or None, a.colour)
