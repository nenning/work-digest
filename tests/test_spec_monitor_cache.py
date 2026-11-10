from digest.spec_monitor.cache import SpecCache, hash_key


def test_text_round_trip_counts_hit(tmp_path):
    cache = SpecCache(tmp_path)
    cache.put_text("pages", "123_v4", "Grüße <p>")
    assert cache.get_text("pages", "123_v4") == "Grüße <p>"
    assert (cache.hits, cache.misses) == (1, 0)


def test_json_round_trip_counts_hit(tmp_path):
    cache = SpecCache(tmp_path)
    cache.put_json("versions", "123", [{"v": 1, "t": "Übersicht"}])
    assert cache.get_json("versions", "123") == [{"v": 1, "t": "Übersicht"}]
    assert (cache.hits, cache.misses) == (1, 0)


def test_persists_across_instances(tmp_path):
    SpecCache(tmp_path).put_text("ns", "k", "v")
    assert SpecCache(tmp_path).get_text("ns", "k") == "v"


def test_missing_entry_is_counted_miss(tmp_path):
    cache = SpecCache(tmp_path)
    assert cache.get_text("ns", "nope") is None
    assert cache.get_json("ns", "nope") is None
    assert (cache.hits, cache.misses) == (0, 2)


def test_corrupt_json_is_a_miss(tmp_path):
    cache = SpecCache(tmp_path)
    cache.put_json("ns", "k", {"a": 1})
    (tmp_path / "ns" / "k.json").write_text("{not json", encoding="utf-8")
    assert cache.get_json("ns", "k") is None
    assert (cache.hits, cache.misses) == (0, 1)


def test_undecodable_bytes_are_a_miss(tmp_path):
    cache = SpecCache(tmp_path)
    cache.put_text("ns", "k", "x")
    cache.put_json("ns", "j", {})
    (tmp_path / "ns" / "k.txt").write_bytes(b"\xff\xfe\x00bad")
    (tmp_path / "ns" / "j.json").write_bytes(b"\xff\xfe\x00bad")
    assert cache.get_text("ns", "k") is None
    assert cache.get_json("ns", "j") is None
    assert cache.misses == 2


def test_empty_json_file_is_a_miss(tmp_path):
    cache = SpecCache(tmp_path)
    cache.put_json("ns", "k", {})
    (tmp_path / "ns" / "k.json").write_text("", encoding="utf-8")
    assert cache.get_json("ns", "k") is None


def test_unsafe_key_cannot_escape_cache_root(tmp_path):
    root = tmp_path / "cache"
    cache = SpecCache(root)
    cache.put_text("ns", "../../evil", "x")
    assert not (tmp_path / "evil.txt").exists()
    assert cache.get_text("ns", "../../evil") == "x"
    assert all(root in p.parents for p in root.rglob("*.txt"))


def test_distinct_unsafe_keys_do_not_collide(tmp_path):
    cache = SpecCache(tmp_path)
    cache.put_text("ns", "a/b", "slash")
    cache.put_text("ns", "a_b", "underscore")
    assert cache.get_text("ns", "a/b") == "slash"
    assert cache.get_text("ns", "a_b") == "underscore"


def test_write_failure_is_swallowed(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the cache root should be", encoding="utf-8")
    cache = SpecCache(blocker / "sub")
    cache.put_text("ns", "k", "v")
    cache.put_json("ns", "k", {"a": 1})
    assert cache.get_text("ns", "k") is None


def test_unserialisable_json_is_swallowed_and_leaves_no_entry(tmp_path):
    cache = SpecCache(tmp_path)
    cache.put_json("ns", "k", {"a": object()})
    assert cache.get_json("ns", "k") is None


def test_hash_key_is_stable_and_order_sensitive():
    assert hash_key("a", "b") == hash_key("a", "b")
    assert len(hash_key("a")) == 32
    assert hash_key("a", "b") != hash_key("b", "a")


def test_hash_key_separates_parts():
    assert hash_key("ab", "c") != hash_key("a", "bc")
    assert hash_key("a", "") != hash_key("a")


def test_counters_are_exact_under_threads(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    cache = SpecCache(tmp_path)
    cache.put_text("ns", "present", "x")

    def work(_):
        for _i in range(300):
            cache.get_text("ns", "present")
            cache.get_json("ns", "absent")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(8)))
    assert (cache.hits, cache.misses) == (2400, 2400)


def test_temp_file_names_are_unique_per_thread(tmp_path, mocker):
    import threading
    from digest.spec_monitor import cache as cache_mod
    spy = mocker.spy(cache_mod.os, "replace")
    cache = SpecCache(tmp_path)
    cache.put_text("ns", "k", "main")
    t = threading.Thread(target=cache.put_text, args=("ns", "k", "worker"))
    t.start()
    t.join()
    sources = [c.args[0].name for c in spy.call_args_list]
    assert len(sources) == 2 and len(set(sources)) == 2


def test_concurrent_writes_to_same_key_are_not_lost(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    cache = SpecCache(tmp_path)
    p = cache._path("ns", "k", ".txt")
    errors = []

    def work(i):
        for n in range(60):
            try:
                SpecCache._write(p, f"{i}-{n}" * 2000)
            except OSError as exc:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(8)))
    assert errors == []
    assert cache.get_text("ns", "k") is not None
    assert list(p.parent.glob("*.tmp")) == []


def test_corrupt_entries_are_counted_but_missing_ones_are_not(tmp_path):
    cache = SpecCache(tmp_path)
    cache.get_text("ns", "absent")
    cache.put_json("ns", "k", {"a": 1})
    (tmp_path / "ns" / "k.json").write_text("{not json", encoding="utf-8")
    cache.get_json("ns", "k")
    assert cache.corrupt == 1


def test_write_failures_are_counted_with_first_message(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the cache directory should be", encoding="utf-8")
    cache = SpecCache(blocker)
    assert cache.put_text("ns", "k", "v") is False
    assert cache.put_json("ns", "k", {}) is False
    assert cache.write_errors == 2
    assert cache.first_write_error
