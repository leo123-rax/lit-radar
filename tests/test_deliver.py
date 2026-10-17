"""Delivery: HTML sanitiser for the e-mail body, verified-TLS SMTP, failure handling."""
import smtplib
import ssl
import sys

import pytest

from litradar.config import Settings
from litradar.digest import deliver


# ---- sanitiser ---------------------------------------------------------------------------------
def test_sanitize_keeps_allow_listed_tags_and_drops_everything_else():
    raw = (
        '<h2 class="x">Top</h2><p onclick="evil()">Hello <b>bold</b> <em>em</em> <sup>1</sup></p>'
        '<script>alert(1)</script><style>p{}</style><iframe src="https://evil.example"></iframe>'
        '<table><thead><tr><th>a</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>'
        '<a href="https://example.org/paper" target="_blank" onmouseover="x()">link</a>'
        '<a href="javascript:alert(1)">bad</a><img src="x" onerror="y()"><div><span>text</span></div><br/>'
        "<!-- comment -->"
    )
    out = deliver.sanitize_html(raw)
    assert out.startswith("<h2>Top</h2><p>Hello <b>bold</b> <em>em</em> <sup>1</sup></p>")
    assert "<script" not in out and "alert(1)" not in out and "<style" not in out and "p{}" not in out and "<iframe" not in out
    assert "onclick" not in out and "onmouseover" not in out and "target=" not in out and "onerror" not in out
    assert '<a href="https://example.org/paper">link</a>' in out
    assert "<a>bad</a>" in out and "javascript:" not in out
    assert "<img" not in out and "<div>" not in out and "<span>" not in out and "text" in out
    assert "<br>" in out and "comment" not in out
    assert "<table><thead><tr><th>a</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>" in out


def test_sanitize_is_case_insensitive_and_keeps_text_entities():
    out = deliver.sanitize_html("<SCRIPT>x</SCRIPT><P>A &amp; B &lt;tag&gt;</P><STRONG>s</STRONG>")
    assert out == "<p>A &amp; B &lt;tag&gt;</p><strong>s</strong>"


def test_markdown_to_html_without_markdown_package_escapes_everything(monkeypatch):
    monkeypatch.setitem(sys.modules, "markdown", None)  # makes `import markdown` raise ImportError
    out = deliver.markdown_to_html("# Title <script>alert(1)</script> & co | a |")
    assert out.startswith("<pre") and "<script" not in out and "&lt;script&gt;" in out and "&amp; co" in out


def test_markdown_to_html_with_markdown_package_is_sanitised():
    pytest.importorskip("markdown")
    md = "| a | b |\n|---|---|\n| 1 | <script>alert(1)</script> |\n\n[x](https://example.org) <img src=x onerror=y()>"
    out = deliver.markdown_to_html(md)
    assert "<table>" in out and "<td>1</td>" in out
    assert "<script" not in out and "<img" not in out and "onerror" not in out
    assert '<a href="https://example.org">x</a>' in out


# ---- SMTP ---------------------------------------------------------------------------------------
class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.starttls_context = None
        self.logged_in = None
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.starttls_context = context

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


def _settings(port=587):
    return Settings(smtp_host="smtp.example.org", smtp_port=port, smtp_user="u", smtp_password="p", digest_from="a@example.org", digest_to="b@example.org, c@example.org")


def test_starttls_uses_a_verifying_default_context(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    assert deliver.send_email(_settings(587), "subj", "# hi\n\nbody") is True
    s = _FakeSMTP.instances[0]
    assert isinstance(s.starttls_context, ssl.SSLContext)
    assert s.starttls_context.verify_mode == ssl.CERT_REQUIRED and s.starttls_context.check_hostname is True
    assert s.logged_in == ("u", "p") and len(s.sent) == 1
    assert s.sent[0]["To"] == "b@example.org, c@example.org"


def test_port_465_uses_smtp_ssl_with_the_same_context(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP", None)  # must not be used
    assert deliver.send_email(_settings(465), "subj", "body") is True
    s = _FakeSMTP.instances[0]
    assert isinstance(s.context, ssl.SSLContext) and s.context.verify_mode == ssl.CERT_REQUIRED
    assert s.starttls_context is None and len(s.sent) == 1


def test_smtp_failure_is_logged_and_returns_false(monkeypatch, caplog):
    class Boom(_FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(smtplib, "SMTP", Boom)
    assert deliver.send_email(_settings(), "subj", "body") is False
    assert "e-mail delivery failed" in caplog.text


def test_unconfigured_email_returns_false():
    assert deliver.send_email(Settings(), "subj", "body") is False
