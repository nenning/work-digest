from digest import problems


def test_take_is_scoped_and_removes():
    problems.clear()
    problems.report("a", "one")
    problems.report("a", "two", lossy=False)
    problems.report("b", "other", lossy=False)
    assert problems.take("a") == (["one", "two"], True)
    assert problems.take("a") == ([], False)
    assert problems.take("b") == (["other"], False)


def test_corrupt_state_is_reported(tmp_path):
    from digest.state import load_state
    problems.clear()
    f = tmp_path / "state.json"
    f.write_text("{not json")
    assert load_state(f) == {}
    msgs, lossy = problems.take("state")
    assert msgs and "unreadable" in msgs[0] and not lossy
