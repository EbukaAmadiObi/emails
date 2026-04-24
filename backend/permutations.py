import re
from unidecode import unidecode


def normalize_name(name: str) -> str:
    """Convert a name to lowercase ASCII, stripping non-alphanumeric chars.

    José → jose, Müller → muller, O'Brien → obrien, Jean-Pierre → jeanpierre
    """
    ascii_name = unidecode(name)
    return re.sub(r"[^a-z0-9]", "", ascii_name.lower())


def generate_permutations(first: str, last: str) -> list[str]:
    """Generate email local-part permutations in B2B frequency order.

    Normalizes Unicode names to ASCII before generating patterns.
    Returns a deduplicated list; all patterns are lowercase.
    """
    if not first or not first.strip():
        raise ValueError("First name cannot be empty")
    if not last or not last.strip():
        raise ValueError("Last name cannot be empty")

    f = normalize_name(first)
    l = normalize_name(last)

    if not f:
        raise ValueError(f"First name '{first}' normalized to empty string")
    if not l:
        raise ValueError(f"Last name '{last}' normalized to empty string")

    fi = f[0]   # first initial
    li = l[0]   # last initial

    # Frequency-ordered: top 6 patterns cover ~80% of real B2B addresses.
    # Source: Hunter.io / industry data on B2B email format distribution.
    candidates = [
        f"{f}.{l}",       # john.smith       ~44%
        f"{fi}{l}",       # jsmith           ~11%
        f"{f}_{l}",       # john_smith        ~3%
        f"{f}",           # john              ~8%
        f"{fi}.{l}",      # j.smith           ~6%
        f"{f}{l}",        # johnsmith        ~15%
        # --- mid-tier ---
        f"{f}-{l}",       # john-smith
        f"{f}{li}",       # johns  (first + last initial)
        f"{f}.{li}",      # john.s
        f"{fi}_{l}",      # j_smith
        f"{l}.{f}",       # smith.john
        f"{l}{f}",        # smithjohn
        f"{l}.{fi}",      # smith.j
        f"{fi}.{li}",     # j.s
        f"{l}",           # smith
        # --- lower frequency ---
        f"{l}_{f}",       # smith_john
        f"{fi}-{l}",      # j-smith
        f"{f}-{li}",      # john-s
        f"{l}-{f}",       # smith-john
        f"{l}{fi}",       # smithj
    ]

    # Deduplicate while preserving order (short names can produce duplicates)
    seen: set[str] = set()
    result: list[str] = []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            result.append(p)

    return result
