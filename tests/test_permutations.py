import pytest
from backend.permutations import generate_permutations, normalize_name


class TestNormalizeName:
    def test_ascii_passthrough(self):
        assert normalize_name("John") == "john"

    def test_unicode_accent(self):
        assert normalize_name("José") == "jose"

    def test_unicode_umlaut(self):
        assert normalize_name("Müller") == "muller"

    def test_hyphen_stripped(self):
        assert normalize_name("Jean-Pierre") == "jeanpierre"

    def test_apostrophe_stripped(self):
        assert normalize_name("O'Brien") == "obrien"

    def test_already_lowercase(self):
        assert normalize_name("smith") == "smith"


class TestGeneratePermutations:
    def test_happy_path_contains_top_patterns(self):
        perms = generate_permutations("John", "Smith")
        assert "john.smith" in perms
        assert "jsmith" in perms
        assert "johnsmith" in perms
        assert "john" in perms
        assert "j.smith" in perms

    def test_order_most_common_first(self):
        perms = generate_permutations("John", "Smith")
        assert perms[0] == "john.smith"
        assert perms[1] == "jsmith"

    def test_no_duplicates(self):
        perms = generate_permutations("John", "Smith")
        assert len(perms) == len(set(perms))

    def test_all_lowercase(self):
        perms = generate_permutations("JOHN", "SMITH")
        for p in perms:
            assert p == p.lower()

    def test_unicode_first_name(self):
        perms = generate_permutations("José", "García")
        assert "jose.garcia" in perms
        assert "jgarcia" in perms

    def test_unicode_umlaut(self):
        perms = generate_permutations("Müller", "Schmidt")
        assert "muller.schmidt" in perms

    def test_hyphenated_name(self):
        perms = generate_permutations("Jean-Pierre", "Dubois")
        assert "jeanpierre.dubois" in perms

    def test_apostrophe_name(self):
        perms = generate_permutations("O'Brien", "Kelly")
        assert "obrien.kelly" in perms

    def test_single_char_first(self):
        perms = generate_permutations("J", "Smith")
        # Single-char first: f == fi, so some patterns deduplicate
        assert "j.smith" in perms
        assert len(perms) == len(set(perms))

    def test_empty_first_raises(self):
        with pytest.raises(ValueError, match="First name cannot be empty"):
            generate_permutations("", "Smith")

    def test_empty_last_raises(self):
        with pytest.raises(ValueError, match="Last name cannot be empty"):
            generate_permutations("John", "")

    def test_whitespace_only_first_raises(self):
        with pytest.raises(ValueError):
            generate_permutations("   ", "Smith")

    def test_non_ascii_only_name_raises(self):
        # A name that normalizes to empty (e.g. only symbols)
        with pytest.raises(ValueError):
            generate_permutations("---", "Smith")

    def test_returns_list_of_strings(self):
        perms = generate_permutations("John", "Smith")
        assert isinstance(perms, list)
        assert all(isinstance(p, str) for p in perms)

    def test_minimum_pattern_count(self):
        perms = generate_permutations("John", "Smith")
        # Expect at least 15 distinct patterns for a normal name
        assert len(perms) >= 15
