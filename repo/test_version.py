from version import get_version


def test_version():
    assert get_version() == '1.2.3'
