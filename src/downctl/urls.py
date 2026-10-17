"""URL display, redaction and content-identity helpers.

Two different questions are answered here:

- **Display**: what may be printed to a terminal, a JSON stream, a webhook or
  an ``exec`` child process? Credentials embedded in the userinfo or in a
  secret query parameter must never reach those places, so
  :func:`display_url` masks them.
- **Identity**: which remote object does a resumable bitmap / completion
  marker belong to? Two URLs that differ only by a rotating signature refer
  to the same object, while two URLs that differ by path or revision do not.

Identity is a SHA-256 of a canonical URL (userinfo removed, fragment dropped;
the query is stripped for presigned URLs via ``redact=True``). Only the digest
is persisted, so the ledger never stores a token or signature on disk.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Query/option names containing any of these fragments are treated as secrets.
_SECRET_HINTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "signature",
    "credential",
    "authorization",
    "auth",
    "key",
    "sig",
)


def is_secret_name(name: str) -> bool:
    """Whether a query parameter / option name looks credential-bearing."""
    lowered = name.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def _strip_userinfo(netloc: str) -> str:
    """Drop ``user:password@`` from a netloc (keeps host and port)."""
    if "@" not in netloc:
        return netloc
    return netloc.rsplit("@", 1)[1]


def redact_url(url: str, *, strip_query: bool = False) -> str:
    """Return a printable form of ``url`` with credentials masked.

    ``strip_query=True`` removes the whole query string (used for presigned
    URLs, whose query is entirely credentials). Otherwise only values of
    secret-looking parameters are replaced, so ordinary URLs stay readable.
    Malformed URLs and schemeless ``user:pass@host`` strings are sanitized too
    rather than returned verbatim.
    """
    if not url:
        return url
    if "://" not in url:
        # Proxies are often written as ``user:pass@host:port`` without a
        # scheme; never echo the password.
        if "@" in url:
            prefix, _sep, rest = url.partition("@")
            if ":" in prefix:
                return rest
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        # e.g. an unterminated IPv6 literal. Strip userinfo and the whole
        # query/fragment rather than risk printing a password or token.
        sanitized = re.sub(r"(?<=://)[^/@]*@", "", url, count=1)
        return re.split(r"[?#]", sanitized, maxsplit=1)[0]
    netloc = _strip_userinfo(parts.netloc)
    if strip_query:
        query = ""
    elif parts.query:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        query = urlencode(
            [(key, "***" if is_secret_name(key) else value) for key, value in pairs],
            safe="*",
        )
    else:
        query = ""
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def display_url(url: str, redact: bool = False) -> str:
    """Printable URL: strip the query for presigned URLs, mask secrets otherwise."""
    return redact_url(url, strip_query=redact)


#: Anything that looks like an absolute URL inside a free-form message.
_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+")
#: Punctuation that usually belongs to the sentence, not to the URL.
_TRAILING_PUNCTUATION = ".,;:)]}"


def redact_text(text: str) -> str:
    """Mask credentials in every URL embedded in an exception/log message.

    Error strings from aiohttp and friends often embed the request URL; a
    presigned query or a ``?token=`` value must not reach a terminal even when
    the exception was not raised by our own code. Non-URL text is untouched.
    """

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in _TRAILING_PUNCTUATION:
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        return redact_url(raw) + trailing

    return _URL_IN_TEXT.sub(_replace, text)


def url_identity(url: str, redact: bool = False) -> str:
    """Stable SHA-256 identity of the *content* a URL points at.

    ``redact=True`` strips the query (S3 presigned URLs rotate their signature
    on every run, but still name the same object). The fragment is always
    ignored because it never reaches the server.
    """
    if "://" in url:
        try:
            parts = urlsplit(url)
        except ValueError:  # pragma: no cover - extremely malformed input
            canonical = url
        else:
            canonical = urlunsplit(
                (
                    parts.scheme,
                    _strip_userinfo(parts.netloc),
                    parts.path,
                    "" if redact else parts.query,
                    "",
                )
            )
    else:
        canonical = url
    return hashlib.sha256(canonical.encode("utf-8", errors="surrogatepass")).hexdigest()


def mask_mapping(data: Mapping[str, object]) -> dict[str, object]:
    """Recursively mask secret-looking keys in a config mapping for display.

    Non-secret values that contain URLs are passed through :func:`display_url`
    as well, so a webhook endpoint with ``?token=`` or a mirror URL with
    userinfo is not exposed by ``downctl config show``.
    """
    masked: dict[str, object] = {}
    for key, value in data.items():
        if is_secret_name(str(key)):
            masked[str(key)] = "***"
        else:
            masked[str(key)] = _mask_value(value)
    return masked


def _mask_value(value: object) -> object:
    if isinstance(value, Mapping):
        return mask_mapping(value)
    if isinstance(value, list):
        return [_mask_value(item) for item in value]
    if isinstance(value, str) and "://" in value:
        return display_url(value)
    return value
