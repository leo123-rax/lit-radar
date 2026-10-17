from litradar.policy import fulltext_storage_level, normalize_license


def test_levels():
    assert fulltext_storage_level("cc_by") == "full"
    assert fulltext_storage_level("cc_by_nc_nd") == "full"
    assert fulltext_storage_level("cc0") == "full"
    assert fulltext_storage_level("cc_no") == "facts_only"
    assert fulltext_storage_level("", open_access=True) == "full"
    assert fulltext_storage_level("", open_access=False) == "facts_only"
    assert fulltext_storage_level(None, text_available=False) == "none"
    assert fulltext_storage_level("https://creativecommons.org/licenses/by-nc/4.0/") == "full"
    assert fulltext_storage_level("https://creativecommons.org/publicdomain/zero/1.0/") == "full"


def test_normalize():
    assert normalize_license("CC BY 4.0") == "cc_by_4.0"
    assert normalize_license("https://creativecommons.org/licenses/by/4.0/") == "cc-by-4.0"
