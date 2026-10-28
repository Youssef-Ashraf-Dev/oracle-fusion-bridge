import pytest

from bridge.app import fusion_hint, split_message


def test_short_text_is_one_piece():
    assert split_message("hello", 4000) == ["hello"]
    assert split_message("   ", 4000) == []


def test_long_text_is_split_without_loss_and_under_limit():
    text = "\n".join(f"Invoice {i}: ACME Ltd, EGP {i * 100}.00, status Approved" for i in range(400))
    assert len(text) > 10_000
    pieces = split_message(text, 4000)
    assert len(pieces) >= 3 and all(len(p) <= 4000 for p in pieces)
    assert "\n".join(pieces).split() == text.split()           # nothing lost, order kept


def test_single_huge_line_is_cut_at_word_boundaries():
    text = ("word " * 3000).strip()
    pieces = split_message(text, 1000)
    assert all(len(p) <= 1000 for p in pieces)
    assert " ".join(pieces).split() == text.split()


@pytest.mark.parametrize("limit", [50, 200, 4000])
def test_never_exceeds_limit(limit):
    text = "x" * 5000 + "\n" + "short line\n" * 300
    assert all(len(p) <= limit for p in split_message(text, limit))


@pytest.mark.parametrize("err,needle", [
    ("Error executing tool: [Errno -2] Name or service not known", "could not be reached"),
    ("HTTP 401 Unauthorized", "rejected"),
    ("403 Forbidden for resource", "not allowed"),
    ("something odd happened", "Fusion reported an error"),
])
def test_fusion_hint_is_plain_language(err, needle):
    assert needle in fusion_hint([err])
    assert fusion_hint([]) == ""
