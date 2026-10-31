from loader import load


def test_retries():
    assert load('retries') == 3


def test_timeout_default():
    assert load('timeout') == 30
