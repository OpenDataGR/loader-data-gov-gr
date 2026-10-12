"""Turning the call a consumer makes into the call the provider expects -- the request side of a live proxy, the counterpart of
reshaping the answer (the Transform Box).

Two directions, each driven by a short template the admin writes:

* callers use GET, the provider wants POST: the caller's query parameters fill ``{name}`` placeholders in a **JSON body**
  template. A value is inserted JSON-escaped, so whatever a caller sends stays one string inside the body and can never change
  its structure; the placeholders therefore have to sit inside quotes (a template whose body is not valid JSON with a dummy value
  in each placeholder is refused when it is saved).
* callers use POST, the provider wants GET: the fields of the caller's JSON body fill ``{path}`` placeholders in a **query
  template** (``plate={plate}&kind={vehicle/type}``), URL-encoded by the HTTP library.

Only what the template names is ever passed on -- a caller's other parameters or fields are dropped. Values must be short
(``MAX_VALUE_LENGTH``) and free of control characters. Every refusal is a ``RequestTemplateError`` whose message names the
parameter, never the value.
"""

from __future__ import annotations

import json
import re

MAX_TEMPLATE_BYTES = 4000
MAX_VALUE_LENGTH = 200
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_/-]{0,40})\}")
_QUERY_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


class RequestTemplateError(ValueError):
    """The call cannot be turned into the provider's call (or the template itself is wrong); the message is safe to show."""


def placeholders(template: str) -> list[str]:
    seen: list[str] = []
    for name in _PLACEHOLDER.findall(template or ""):
        if name not in seen:
            seen.append(name)
    return seen


def _clean(value: object, name: str) -> str:
    text = value if isinstance(value, str) else (str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None)
    if text is None:
        raise RequestTemplateError("'%s' must be text or a number." % name)
    if len(text) > MAX_VALUE_LENGTH:
        raise RequestTemplateError("'%s' is longer than %d characters." % (name, MAX_VALUE_LENGTH))
    if any(ord(c) < 32 for c in text):
        raise RequestTemplateError("'%s' contains a control character." % name)
    return text


# -- callers GET, provider POST: query parameters -> a JSON body -----------------------------------------------------------------

def build_json_body(template: str, params: dict) -> bytes:
    """The JSON body for the provider. Raises ``RequestTemplateError`` for a missing or unusable parameter."""
    def fill(match: re.Match) -> str:
        name = match.group(1)
        if name not in params:
            raise RequestTemplateError("The parameter '%s' is missing." % name)
        return json.dumps(_clean(params[name], name), ensure_ascii=False)[1:-1]  # escaped, to sit inside the template's quotes

    body = _PLACEHOLDER.sub(fill, template)
    try:
        json.loads(body)
    except ValueError:
        raise RequestTemplateError("The request could not be built.") from None
    return body.encode("utf-8")


def validate_json_template(template: str) -> None:
    template = (template or "").strip()
    if not template:
        raise RequestTemplateError("Write the JSON body the provider expects, with {name} where a caller's value goes.")
    if len(template.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise RequestTemplateError("The template is larger than %d bytes." % MAX_TEMPLATE_BYTES)
    names = placeholders(template)
    if not names:
        raise RequestTemplateError("The template has no {name} placeholder, so callers could not choose anything.")
    try:
        parsed = json.loads(_PLACEHOLDER.sub("x", template))
    except ValueError as exc:
        raise RequestTemplateError("The template is not valid JSON once the placeholders hold text (%s). Put each {name} inside quotes." % exc) from None
    if not isinstance(parsed, (dict, list)):
        raise RequestTemplateError("The body must be a JSON object or list.")


# -- callers POST, provider GET: a JSON body -> query parameters ------------------------------------------------------------------

def _pairs(template: str) -> list[tuple[str, str]]:
    pairs = []
    for part in re.split(r"[&\n]+", template or ""):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not _QUERY_KEY.match(key):
            raise RequestTemplateError("'%s' is not a name=value pair (names: letters, digits, '.', '_', '-')." % part[:40])
        pairs.append((key, value))
    return pairs


def validate_query_template(template: str) -> None:
    template = (template or "").strip()
    if not template:
        raise RequestTemplateError("Write the query parameters the provider expects, e.g. plate={plate}.")
    if len(template.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise RequestTemplateError("The template is larger than %d bytes." % MAX_TEMPLATE_BYTES)
    if not _pairs(template):
        raise RequestTemplateError("The template has no name=value pair.")


def _lookup(body: object, path: str) -> object:
    current = body
    for part in path.split("/"):
        if not isinstance(current, dict) or part not in current:
            raise RequestTemplateError("The field '%s' is missing." % path)
        current = current[part]
    return current


def build_query(template: str, body: object) -> dict:
    """The query parameters for the provider, from the caller's JSON body (an object)."""
    if not isinstance(body, dict):
        raise RequestTemplateError("The body must be a JSON object.")
    out: dict = {}
    for key, value in _pairs(template):
        out[key] = _PLACEHOLDER.sub(lambda m: _clean(_lookup(body, m.group(1)), m.group(1)), value)
    return out
