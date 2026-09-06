#!/usr/bin/env python3
"""Validate free-nyt-cooking JSONL shards against schema/recipe.schema.json.

Stdlib-only (a small JSON Schema draft-2020-12 subset covering every construct
used by recipe.schema.json) so CI needs nothing but Python.

Usage:
    python3 tools/validate.py [data/recipes/ | shard.jsonl ...] [--schema PATH]

Exit code 0 = every line valid. Errors print to stderr as
<file>:<line>: <path>: <message>.
"""
import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SCHEMA = os.path.join(REPO_ROOT, "schema", "recipe.schema.json")

_URI_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\S+$")
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def _type_ok(value, t):
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    return True  # unknown type keyword: don't fail


def validate(instance, schema, path="$", errors=None):
    """Minimal JSON Schema (2020-12 subset) validator. Appends error strings."""
    if errors is None:
        errors = []
    if not isinstance(schema, dict):
        return errors

    if "type" in schema:
        types = schema["type"]
        types = types if isinstance(types, list) else [types]
        if not any(_type_ok(instance, t) for t in types):
            errors.append("%s: expected type %s, got %s"
                          % (path, "/".join(types), type(instance).__name__))
            return errors  # type mismatch makes deeper checks noise

    if "enum" in schema and instance not in schema["enum"]:
        errors.append("%s: %r not in enum %r" % (path, instance, schema["enum"]))

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append("%s: shorter than minLength %d" % (path, schema["minLength"]))
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append("%s: %r does not match pattern %s"
                          % (path, instance, schema["pattern"]))
        fmt = schema.get("format")
        if fmt == "uri" and not _URI_RE.match(instance):
            errors.append("%s: %r is not a uri" % (path, instance))
        elif fmt == "date-time" and not _DATETIME_RE.match(instance):
            errors.append("%s: %r is not a date-time" % (path, instance))

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append("%s: %r < minimum %r" % (path, instance, schema["minimum"]))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append("%s: %r > maximum %r" % (path, instance, schema["maximum"]))

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append("%s: fewer than minItems %d" % (path, schema["minItems"]))
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(instance):
                validate(item, item_schema, "%s[%d]" % (path, i), errors)

    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                errors.append("%s: missing required property %r" % (path, req))
        props = schema.get("properties", {})
        for key, subschema in props.items():
            if key in instance:
                validate(instance[key], subschema, "%s.%s" % (path, key), errors)
        addl = schema.get("additionalProperties", True)
        if addl is False:
            for key in instance:
                if key not in props:
                    errors.append("%s: additional property %r not allowed" % (path, key))
        elif isinstance(addl, dict):
            for key in instance:
                if key not in props:
                    validate(instance[key], addl, "%s.%s" % (path, key), errors)
    return errors


def iter_jsonl_files(paths):
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.endswith(".jsonl"):
                    yield os.path.join(p, name)
        elif os.path.isfile(p):
            yield p
        else:
            print("warning: %s not found, skipping" % p, file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=[os.path.join(REPO_ROOT, "data", "recipes")],
                    help="Shard directory or .jsonl files (default: data/recipes/)")
    ap.add_argument("--schema", default=DEFAULT_SCHEMA)
    ap.add_argument("--no-layout-check", action="store_true",
                    help="Skip shard-name/sort-order checks (schema only)")
    args = ap.parse_args(argv)

    schema = json.load(open(args.schema))
    total = 0
    bad = 0
    warnings = 0

    for path in iter_jsonl_files(args.paths or [os.path.join(REPO_ROOT, "data", "recipes")]):
        shard_key = os.path.splitext(os.path.basename(path))[0]
        # per-crawler shards are <prefix>.<crawler>.jsonl (e.g. ab.a.jsonl);
        # the layout check uses the 2-char prefix either way
        if re.match(r"^[a-z0-9]{2}\.[a-z]$", shard_key):
            shard_key = shard_key[:2]
        prev_slug = None
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    bad += 1
                    print("%s:%d: invalid JSON: %s" % (path, lineno, e), file=sys.stderr)
                    continue
                errors = validate(record, schema)
                for e in errors:
                    bad += 1
                    print("%s:%d: %s" % (path, lineno, e), file=sys.stderr)
                if not args.no_layout_check and not errors:
                    slug = record.get("slug", "")
                    if not slug.startswith(shard_key):
                        warnings += 1
                        print("%s:%d: warning: slug %r does not match shard key %r"
                              % (path, lineno, slug, shard_key), file=sys.stderr)
                    if prev_slug is not None and slug < prev_slug:
                        warnings += 1
                        print("%s:%d: warning: slug %r out of sort order"
                              % (path, lineno, slug), file=sys.stderr)
                    prev_slug = slug

    print("validated %d record(s): %d error(s), %d layout warning(s)"
          % (total, bad, warnings), file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
