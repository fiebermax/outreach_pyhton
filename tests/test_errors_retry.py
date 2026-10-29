import asyncio

import pytest

from core.errors import (
    BlockedError, FetchTimeout, HttpClientError, RateLimited, ServerError,
    error_for_http_status, is_temporary,
)
from core.retry import backoff_delay, retry_async, retry_call


@pytest.mark.parametrize("status,expected", [
    (200, None), (301, None), (404, HttpClientError), (403, HttpClientError),
    (429, RateLimited), (500, ServerError), (503, ServerError),
])
def test_http_status_mapping(status, expected):
    err = error_for_http_status(status, "https://example.org")
    assert (err is None) if expected is None else isinstance(err, expected)


def test_classification():
    assert is_temporary(FetchTimeout("x"))
    assert is_temporary(RateLimited("x"))
    assert not is_temporary(BlockedError("x"))
    assert not is_temporary(ValueError("x"))


def test_backoff_grows_and_caps():
    assert [backoff_delay(a, 1, 5) for a in (1, 2, 3, 4)] == [1, 2, 4, 5]


class Flaky:
    def __init__(self, errors, result="ok"):
        self.errors = list(errors)
        self.calls = 0
        self.result = result

    def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


def test_temporary_error_is_retried_until_success():
    sleeps = []
    fn = Flaky([FetchTimeout("t"), ServerError("s")])
    assert retry_call(fn, attempts=3, jitter=0, sleep=sleeps.append) == "ok"
    assert fn.calls == 3
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_max_attempts():
    fn = Flaky([FetchTimeout("t")] * 5)
    with pytest.raises(FetchTimeout):
        retry_call(fn, attempts=3, sleep=lambda _: None)
    assert fn.calls == 3


@pytest.mark.parametrize("error", [BlockedError("captcha"), HttpClientError("404"), ValueError("bug")])
def test_permanent_and_unknown_errors_not_retried(error):
    fn = Flaky([error])
    with pytest.raises(type(error)):
        retry_call(fn, attempts=5, sleep=lambda _: None)
    assert fn.calls == 1


def test_rate_limit_respects_retry_after():
    sleeps = []
    retry_call(Flaky([RateLimited("429", retry_after=7)]), jitter=0, sleep=sleeps.append)
    assert sleeps == [7]


def test_async_variant():
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)

    fn = Flaky([FetchTimeout("t")])

    async def call():
        return fn()

    assert asyncio.run(retry_async(call, attempts=2, jitter=0, sleep=fake_sleep)) == "ok"
    assert fn.calls == 2 and sleeps == [1.0]
