from flatbot.store import SeenStore


def test_empty_store_contains_nothing(tmp_path):
    store = SeenStore(str(tmp_path / "seen.txt"))
    assert store.contains("flatfox:123") is False


def test_add_and_contains(tmp_path):
    store = SeenStore(str(tmp_path / "seen.txt"))
    store.add("flatfox:123")
    assert store.contains("flatfox:123") is True
    assert store.contains("homegate:456") is False


def test_persists_across_restarts(tmp_path):
    path = str(tmp_path / "seen.txt")

    store1 = SeenStore(path)
    store1.add("flatfox:abc")
    store1.add("homegate:xyz")

    store2 = SeenStore(path)
    assert store2.contains("flatfox:abc") is True
    assert store2.contains("homegate:xyz") is True
    assert store2.contains("flatfox:other") is False


def test_file_format_is_one_uid_per_line(tmp_path):
    path = str(tmp_path / "seen.txt")
    store = SeenStore(path)
    store.add("flatfox:1")
    store.add("flatfox:2")

    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    assert lines == ["flatfox:1", "flatfox:2"]


def test_loads_existing_file(tmp_path):
    path = str(tmp_path / "seen.txt")
    with open(path, "w") as f:
        f.write("flatfox:existing\nhomegate:also-existing\n")

    store = SeenStore(path)
    assert store.contains("flatfox:existing") is True
    assert store.contains("homegate:also-existing") is True
    assert store.contains("flatfox:unknown") is False


def test_add_is_idempotent(tmp_path):
    path = str(tmp_path / "seen.txt")
    store = SeenStore(path)
    store.add("flatfox:dup")
    store.add("flatfox:dup")

    assert store.contains("flatfox:dup") is True
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    # File may contain duplicates (append-only) — that's fine; the set dedupes on read
    assert "flatfox:dup" in lines
