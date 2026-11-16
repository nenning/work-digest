"""Page titles written as comments behind the page ids in config.yaml."""
from digest.config_annotate import annotate_config_file, annotate_text, page_ids

TITLES = {"111": "Coremodul", "222": "Berichte"}


def test_adds_title_and_keeps_existing_comment_text():
    text = '  parent_page_id: "111"          # specs are searched below this page\n'
    assert annotate_text(text, TITLES) == (
        '  parent_page_id: "111"          # «Coremodul» specs are searched below this page\n')


def test_line_without_comment_gets_one():
    assert annotate_text("  parent_page_id: 222\n", TITLES) == "  parent_page_id: 222  # «Berichte»\n"


def test_renamed_page_replaces_old_title_only():
    text = '  parent_page_id: "111"  # «Old name» keep this\n'
    assert annotate_text(text, TITLES) == '  parent_page_id: "111"  # «Coremodul» keep this\n'


def test_older_hand_written_title_prefix_is_converted():
    text = '  parent_page_id: "111"  # Coremodul: this page is watched\n'
    assert annotate_text(text, TITLES) == '  parent_page_id: "111"  # «Coremodul» this page is watched\n'


def test_idempotent():
    once = annotate_text('  parent_page_id: "111"  # note\n', TITLES)
    assert annotate_text(once, TITLES) == once


def test_unknown_id_commented_out_and_other_lines_untouched():
    text = ('  api_token: "x"  # secret\n'
            '  # parent_page_id: "111"\n'
            '  parent_page_id: "999"  # keep\n'
            '  publish_parent_page_id: "111"\r\n')
    out = annotate_text(text, TITLES)
    assert out == text.replace('"111"\r\n', '"111"  # «Coremodul»\r\n')


def test_page_ids_deduplicated():
    assert page_ids('a:\n  parent_page_id: "1"\n  confluence_page:\n    parent_page_id: "1"\n    x_page_id: 2\n') == ["1", "2"]


def test_file_rewritten_with_encoding_preserved_and_lookup_errors_reported(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes('name: Zürich\nparent_page_id: "111"\nother_page_id: "999"\n'.encode("cp1252"))

    def get_title(page_id):
        if page_id == "999":
            raise LookupError("page 999 not found (HTTP 404)")
        return "Über  uns"

    problems = annotate_config_file(path, get_title)
    assert path.read_bytes().decode("cp1252") == (
        'name: Zürich\nparent_page_id: "111"  # «Über uns»\nother_page_id: "999"\n')
    assert len(problems) == 1 and "999" in problems[0]
    assert not (tmp_path / "config.yaml.tmp").exists()


def test_file_untouched_when_nothing_changes(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('parent_page_id: "111"  # «Coremodul»\n', encoding="utf-8")
    before = path.stat().st_mtime_ns
    assert annotate_config_file(path, lambda _id: "Coremodul") == []
    assert path.stat().st_mtime_ns == before
