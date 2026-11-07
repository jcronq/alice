from alice_forge.dispatcher.effort import read_effort


def test_default_medium():
    assert read_effort({"labels": []}) == "medium"


def test_label():
    assert read_effort({"labels": [{"name": "effort:max"}]}) == "max"


def test_verb_overrides_label():
    issue = {
        "labels": [{"name": "effort:low"}],
        "comments": [{"body": "[SM] effort=high"}],
    }
    assert read_effort(issue) == "high"


def test_invalid_label_falls_back():
    assert read_effort({"labels": [{"name": "effort:bogus"}]}) == "medium"
