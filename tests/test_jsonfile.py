"""The one decoder every hand-editable JSON file is read through."""

from __future__ import annotations

import codecs
import json

import pytest

from quickcode import jsonfile

TEXT = json.dumps({"rule": "bash(rm café*)", "n": [1, 2]}, ensure_ascii=False)


@pytest.mark.parametrize("raw", [
    TEXT.encode("utf-8"),
    TEXT.encode("utf-8-sig"),
    codecs.BOM_UTF16_LE + TEXT.encode("utf-16-le"),
    codecs.BOM_UTF16_BE + TEXT.encode("utf-16-be"),
    codecs.BOM_UTF32_LE + TEXT.encode("utf-32-le"),
    codecs.BOM_UTF32_BE + TEXT.encode("utf-32-be"),
], ids=["utf-8", "utf-8-bom", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
def test_the_byte_order_mark_names_the_encoding(tmp_path, raw):
    path = tmp_path / "settings.json"
    path.write_bytes(raw)
    assert jsonfile.load(path) == json.loads(TEXT)


@pytest.mark.parametrize("raw", [
    TEXT.encode("latin-1"),
    TEXT.encode("utf-16-le"),
    codecs.BOM_UTF16_LE + b"{\x00\x00",
], ids=["latin-1", "utf-16-without-a-mark", "truncated-utf-16"])
def test_without_a_usable_mark_nothing_is_guessed(tmp_path, raw):
    path = tmp_path / "settings.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        jsonfile.load(path)


def test_a_missing_file_is_an_os_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        jsonfile.load(tmp_path / "absent.json")
