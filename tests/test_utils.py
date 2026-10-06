import threading

import utils

URL = "https://feldenkraisproject.s3.us-east-2.amazonaws.com/Simple_Twisting.mp3"


def test_file_tag_roundtrip():
    name = utils.filename_from_url(URL, index=15, slug="simple-twisting")
    assert utils.file_tag(name) == utils.url_tag(URL)
    assert utils.file_tag("manifest.jsonl") is None


def test_shifted_index_renames_instead_of_redownloading(tmp_path, monkeypatch):
    old = tmp_path / utils.filename_from_url(URL, index=15, slug="simple-twisting")
    old.write_bytes(b"x")
    monkeypatch.setattr(utils, "get_requests_session", lambda: (_ for _ in ()).throw(AssertionError("network used")))
    result = utils._download_one(
        URL, str(tmp_path), str(tmp_path / "m.jsonl"), threading.Lock(), index=16, slug="simple-twisting"
    )
    assert result == str(tmp_path / utils.filename_from_url(URL, index=16, slug="simple-twisting"))
    assert not old.exists() and (tmp_path / "0016_simple-twisting_99cdab.mp3").exists()
