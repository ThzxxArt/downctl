"""URL display, redaction and content-identity helpers.

Two different questions are answered here:

- **Display**: what may be printed to a terminal, a JSON stream, a webhook or
  an ``exec`` child process? Credentials embedded in the userinfo or in a
  secret query parameter must never reach those places, so
  :func:`display_url` masks them.
- **Identity**: which remote object does a resumable bitmap / completion
  marker belong to? Two URLs that differ only by a rotating signature refer
  to the same object, while two URLs that differ by path or revision do not.
- **Version**: is the object *behind* that URL still the same revision?
  Identity answers "same address"; a version witness answers "same bytes".
  Both are needed: the address can stay while the content is replaced (a
  re-uploaded release asset, a rewritten model shard).

Identity is a SHA-256 of a canonical URL (userinfo removed, fragment dropped;
the query is stripped for presigned URLs via ``redact=True``). Only the digest
is persisted, so the ledger never stores a token or signature on disk.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Query/option names that are *exactly* credential-bearing. A substring rule
#: was wrong in both directions: it masked ``token_to_mirror`` (a boolean) and
#: ``design``/``author`` (the hints ``sig``/``auth``), while missing ``code``,
#: ``jwt`` and ``session``.
_SECRET_NAMES: frozenset[str] = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "auth",
        "authorization",
        "password",
        "passwd",
        "passphrase",
        "key_passphrase",
        "secret",
        "secret_key",
        "client_secret",
        "access_key",
        "api_key",
        "apikey",
        "key",
        "private_key",
        "signature",
        "sig",
        "credential",
        "credentials",
        "session",
        "session_id",
        "session_token",
        "jwt",
        "code",
        "x_amz_signature",
        "x_amz_credential",
        "x_amz_security_token",
        "x_goog_signature",
    }
)
#: Suffixes that mark a name as credential-bearing (``hf_token``, ``db_password``).
_SECRET_SUFFIXES: tuple[str, ...] = (
    "_token",
    "_secret",
    "_password",
    "_passwd",
    "_passphrase",
    "_key",
    "_signature",
    "_credential",
)
#: Names that contain a hint but are not secrets.
_NON_SECRET_NAMES: frozenset[str] = frozenset({"token_to_mirror", "keyring", "keywords"})


def is_secret_name(name: str) -> bool:
    """Whether a query parameter / option name is credential-bearing."""
    lowered = name.lower().replace("-", "_")
    if lowered in _NON_SECRET_NAMES:
        return False
    if lowered in _SECRET_NAMES:
        return True
    return lowered.endswith(_SECRET_SUFFIXES)


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
    path = _mask_path_secrets(parts.path)
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
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def _mask_path_secrets(path: str) -> str:
    """Mask the segment that follows a credential-named one (``/token/<v>``).

    Path-based credentials are less common than query ones but just as printable;
    only the *value after a named segment* is touched, so ordinary paths
    (``/v1/models/llama``) are left alone. The value is skipped after masking:
    a token whose text happens to look like a name (``secret``) is a value, not
    another name, and must not cause the next segment to be masked too.
    """
    segments = path.split("/")
    index = 0
    while index < len(segments) - 1:
        if segments[index] and is_secret_name(segments[index]) and segments[index + 1]:
            segments[index + 1] = "***"
            index += 2
            continue
        index += 1
    return "/".join(segments)


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


#: Longest server-supplied validator we are willing to persist. A broken or
#: hostile server could otherwise inflate every sidecar file it touches.
MAX_VALIDATOR_LENGTH = 200
#: Characters that must never reach a saved state file or a request header.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Witness:
    """The version witness a server offers for one remote object.

    ``value`` is the comparison key persisted in resume state: two responses
    describe the same object version when their ``value`` values are equal.
    ``if_range`` is the value that may be sent in an ``If-Range`` request
    header; RFC 9110 forbids a *weak* validator there, so a weak ETag yields
    ``if_range=""`` (the witness stays usable for comparison, just not for a
    conditional range request).

    A witness is a **change detector, not a checksum**: an ETag match does not
    prove the bytes are the ones you already have, it proves the server still
    names the same revision. ``--checksums`` remains the cryptographic proof.
    """

    value: str = ""
    if_range: str = ""
    #: ``"etag"``, ``"last-modified"`` or ``"none"`` (for diagnostics).
    kind: str = "none"

    def __bool__(self) -> bool:
        return bool(self.value)


def _lookup(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup (plain dicts are case-sensitive)."""
    value = headers.get(name)
    if value is not None:
        return str(value)
    lowered = name.lower()
    for key, item in headers.items():
        if str(key).lower() == lowered:
            return str(item)
    return ""


def _clean_validator(value: str) -> str:
    """Trim a header value and refuse anything unusable as a witness."""
    value = value.strip()
    if not value or len(value) > MAX_VALIDATOR_LENGTH or _CONTROL_RE.search(value):
        return ""
    return value


def normalize_validator(headers: Mapping[str, str] | None) -> Witness:
    """Derive a version witness from response headers.

    A strong ``ETag`` wins (it is the only validator precise enough to drive
    ``If-Range``). A weak ETag (``W/"..."``) is still used as the comparison
    key, but with ``if_range`` left empty because RFC 9110 requires a strong
    validator for ``If-Range``. ``Last-Modified`` is the fallback. Servers that
    offer neither yield an empty witness, which callers must treat as
    "unversioned" rather than as "unchanged".
    """
    if not headers:
        return Witness()
    etag = _clean_validator(_lookup(headers, "ETag"))
    if etag:
        weak = etag[:2].upper() == "W/"
        value = _clean_validator(etag[2:] if weak else etag)
        if value:
            return Witness(value=value, if_range="" if weak else value, kind="etag")
    modified = _clean_validator(_lookup(headers, "Last-Modified"))
    if modified:
        return Witness(value=modified, if_range=modified, kind="last-modified")
    return Witness()


def header_value(headers: Mapping[str, str] | None, name: str) -> str:
    """Case-insensitive header read that tolerates ``None``."""
    if not headers:
        return ""
    return _lookup(headers, name)


def mask_mapping(
    data: Mapping[str, object], *, extra_secrets: tuple[str, ...] = ()
) -> dict[str, object]:
    """Recursively mask secret-looking keys in a config mapping for display.

    Non-secret values that contain URLs are passed through :func:`display_url`
    as well, so a webhook endpoint with ``?token=`` or a mirror URL with
    userinfo is not exposed by ``downctl config show``.

    ``extra_secrets`` names keys whose *value* may embed a credential even
    though the key itself does not look secret (``[auth] command = "echo
    ghp_..."``): masking the whole value is safer than trying to find the
    token inside an arbitrary command line.
    """
    hidden = {name.lower() for name in extra_secrets}
    masked: dict[str, object] = {}
    for key, value in data.items():
        if str(key).lower() in hidden or is_secret_name(str(key)):
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
