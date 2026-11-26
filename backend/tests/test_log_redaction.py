"""Log redaction (logging.py) and the request log line (security.py)."""

import importlib
import io
import logging
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import Request

import ahoy.logging as ahoy_logging
from ahoy.app import create_app
from ahoy.config import Settings
from ahoy.logging import UNLOGGABLE, RedactingFilter, install_logging, redact_log

GHU = "ghu_" + "A1b2C3d4E5f6G7h8I9j0" * 2
GHS = "ghs_" + "Zz9" * 12
PAT = "github_pat_11ABCDEFG0_" + "x" * 40
SID = "s3cr3t-c00kie-VALUE-abcdefghijklmnop"
BASIC = "dXNlcjpwYXNzd29yZC1sb25n"
SECRETS = (GHU, GHS, PAT, SID, BASIC, "x" * 40)


@pytest.fixture(autouse=True)
def _restore_raise_exceptions() -> Iterator[None]:
    """A prod app sets logging.raiseExceptions = False process-wide; undo it after each test."""
    saved = logging.raiseExceptions
    yield
    logging.raiseExceptions = saved


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.stream = io.StringIO()
        self.setFormatter(logging.Formatter("%(name)s %(message)s"))
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.stream.write(self.format(record) + "\n")

    @property
    def text(self) -> str:
        return self.stream.getvalue()


@pytest.fixture
def capture() -> Iterator[Capture]:
    install_logging()
    handler = Capture()
    root = logging.getLogger()
    old = root.level
    root.handlers.insert(0, handler)  # first: no other handler's filter runs before it
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(old)


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


@pytest.mark.parametrize(
    "raw",
    [
        f"token {GHU}",
        f"server token {GHS}",
        f"pat {PAT}",
        f"Authorization: Bearer {GHU}",
        f"authorization=token {GHU}",
        f"Authorization: Basic {BASIC}",
        f"Cookie: theme=dark; ahoy_sid={SID}; other=1",
        f"Cookie: __Host-ahoy_sid={SID}",
        f"Set-Cookie: __Host-ahoy_preauth={SID}; Path=/; Secure; HttpOnly",
        f"Set-Cookie: ahoy_preauth={SID}; Path=/",
        f"{{'ahoy_sid': '{SID}'}}",
        f'{{"ahoy_sid": "{SID}"}}',
        f'__Host-ahoy_sid="{SID}"',
        f"{{'__Host-ahoy_preauth': '{SID}'}}",
        f'{{"ahoy_preauth": "{SID}"}}',
        f'ahoy_preauth="{SID}"',
        f"{{'authorization': '{SID}'}}",
        f'{{"Authorization": "{SID}"}}',
        f"{{'Authorization': 'token {SID}'}}",
    ],
)
def test_redact_log_patterns(raw: str) -> None:
    out = redact_log(raw)
    _assert_clean(out)
    assert "REDACTED" in out
    assert redact_log(out) == out  # idempotent


def test_redact_keeps_cookie_names_and_other_text() -> None:
    out = redact_log(f"Cookie: theme=dark; ahoy_sid={SID}")
    assert out == "Cookie: theme=dark; ahoy_sid=[REDACTED]"


def test_redacted_in_msg(capture: Capture) -> None:
    logging.getLogger("ahoy.test").warning(f"cookie ahoy_sid={SID}; Authorization: Bearer {GHU}")
    _assert_clean(capture.text)
    assert "ahoy_sid=[REDACTED]" in capture.text


def test_redacted_in_args(capture: Capture) -> None:
    log = logging.getLogger("ahoy.test.deep.child")  # propagates to root's handler
    log.info("headers %s and %s", {"authorization": f"Bearer {GHU}"}, f"__Host-ahoy_sid={SID}")
    log.info("mapping %(tok)s", {"tok": PAT})
    _assert_clean(capture.text)
    for record in capture.records:
        _assert_clean(record.getMessage())


def test_redacted_in_exception(capture: Capture) -> None:
    log = logging.getLogger("ahoy.test")
    try:
        raise RuntimeError(f"GitHub said no; Cookie: ahoy_sid={SID}; token {GHU}")
    except RuntimeError:
        log.exception("refresh failed for %s", GHS)
    assert "RuntimeError" in capture.text  # the traceback is still there
    _assert_clean(capture.text)
    record = capture.records[-1]
    assert record.exc_info is None
    assert record.exc_text is not None


def test_filter_on_uvicorn_logger_and_its_handlers() -> None:
    f = install_logging()
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        assert f in logging.getLogger(name).filters
    # A handler that uvicorn adds later still never sees a raw record (record factory).
    handler = Capture()
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(handler)
    old = logger.level
    logger.setLevel(logging.INFO)
    try:
        logger.info("upstream %s", f"Bearer {GHU}")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old)
    _assert_clean(handler.text)


def test_filter_is_safe_on_bad_format() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%d %s", (GHU,), None)
    assert RedactingFilter().filter(record)
    _assert_clean(record.getMessage())


# --- request log line -----------------------------------------------------------------

LINE = re.compile(
    r"^ahoy\.request request user=(?P<user>\S+) method=(?P<method>[A-Z]+) "
    r"route=(?P<route>\S+) status=(?P<status>\d{3}) latency_ms=(?P<ms>\d+)$"
)


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {"ahoy_env": "test", "database_url": None}
    return Settings(_env_file=None, **(base | kw))  # type: ignore[call-arg]


async def test_request_log_line_has_only_allowed_fields(capture: Capture) -> None:
    app = create_app(_settings(), "mock", "mock")

    @app.get("/api/things/{thing_id}")
    async def thing(thing_id: str, request: Request) -> dict[str, str]:
        request.state.user_id = "u-123"
        return {"id": thing_id}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get(
            "/api/things/abc-42?secret=querystring",
            headers={"Authorization": f"Bearer {GHU}", "Cookie": f"ahoy_sid={SID}"},
        )
        assert resp.status_code == 200
        await c.get("/api/missing", headers={"User-Agent": "ua-marker"})
        await c.post("/api/things/x", headers={"Origin": "https://evil.example"})

    lines = [ln for ln in capture.text.splitlines() if ln.startswith("ahoy.request ")]
    assert len(lines) == 3, capture.text
    matches = [LINE.match(ln) for ln in lines]
    assert all(matches), lines
    first, second, third = (m.groupdict() for m in matches if m)
    assert first | {"ms": "-"} == {
        "user": "u-123",
        "method": "GET",
        "route": "/api/things/{thing_id}",
        "status": "200",
        "ms": "-",
    }
    assert (second["user"], second["route"], second["status"]) == ("-", "<unmatched>", "404")
    assert (third["method"], third["route"], third["status"]) == ("POST", "<unmatched>", "403")
    joined = "\n".join(lines)
    for leak in ("querystring", "abc-42", "/api/missing", "ua-marker", "testserver", GHU, SID):
        assert leak not in joined


def test_new_root_handler_never_sees_raw_record() -> None:
    install_logging()
    handler = Capture()
    root = logging.getLogger()
    old = root.level
    # Added after install, with no filter of its own, and first in line: no other handler's
    # filter has touched the (shared) record before it.
    root.handlers.insert(0, handler)
    root.setLevel(logging.DEBUG)
    try:
        logging.getLogger("ahoy.some.child").info("refresh %s", f"Bearer {GHU}")
        logging.getLogger("ahoy.some.child").info(f"token {GHS}")
    finally:
        root.removeHandler(handler)
        root.setLevel(old)
    assert handler.records
    _assert_clean(handler.text)


def test_extra_fields_redacted() -> None:
    install_logging()
    handler = Capture()
    handler.setFormatter(logging.Formatter("%(message)s %(tok)s %(hdrs)s"))
    logger = logging.getLogger("ahoy.extra.child")
    old = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("x", extra={"tok": GHU, "hdrs": {"Cookie": f"ahoy_sid={SID}"}})
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old)
    assert "[REDACTED]" in handler.text
    _assert_clean(handler.text)


def test_stack_info_redacted(capture: Capture) -> None:
    # A token in source text shows up in stack_info (the traceback prints source lines).
    logging.getLogger("ahoy.test").info("where", stack_info=True)  # ghu_InSourceLine123456789
    record = capture.records[-1]
    assert record.stack_info is not None
    assert "ghu_InSourceLine123456789" not in record.stack_info
    assert "ghu_InSourceLine123456789" not in capture.text


def test_prod_disables_raise_exceptions() -> None:
    old = logging.raiseExceptions
    try:
        logging.raiseExceptions = True
        install_logging(_settings())
        assert logging.raiseExceptions is True
        prod = _settings()
        prod.ahoy_env = "prod"  # validators do not re-run on assignment
        install_logging(prod)
        assert logging.raiseExceptions is False
    finally:
        logging.raiseExceptions = old


class BadRepr:
    def __repr__(self) -> str:
        raise RuntimeError("repr boom")


class BadStr:
    def __str__(self) -> str:
        raise RuntimeError("str boom")


@pytest.mark.parametrize(
    ("msg", "args", "extra"),
    [
        (BadStr(), (), None),
        ("%r " + GHU, (BadRepr(),), None),
        ("x " + GHU, (), {"d": [BadRepr()], "tok": GHU}),
    ],
    ids=["bad-str-msg", "bad-repr-arg", "bad-repr-extra"],
)
def test_unloggable_record_never_raises_drops_or_leaks(
    capture: Capture, msg: Any, args: tuple[Any, ...], extra: dict[str, Any] | None
) -> None:
    logging.getLogger("ahoy.test").warning(msg, *args, extra=extra)  # must not raise
    record = capture.records[-1]
    assert record.msg == UNLOGGABLE
    assert record.args is None and record.exc_info is None and record.stack_info is None
    assert UNLOGGABLE in capture.text
    _assert_clean(capture.text)
    if extra:
        assert record.tok == "[REDACTED]"  # type: ignore[attr-defined]


def test_install_is_reload_safe(capture: Capture) -> None:
    install_logging()
    wrapper = logging.Logger.makeRecord
    reloaded = importlib.reload(ahoy_logging)
    reloaded.install_logging()
    reloaded.install_logging()
    assert logging.Logger.makeRecord is wrapper  # not wrapped again
    assert not getattr(reloaded._original_make_record, "_ahoy_wrapper", False)
    logging.getLogger("ahoy.test").info("tok %s", GHU)  # no recursion
    _assert_clean(capture.text)
