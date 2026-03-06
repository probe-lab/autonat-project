#!/usr/bin/env python3
"""Evaluate assertions against OTEL trace output.

Usage: echo '<assertions_json>' | python3 eval-assertions.py <trace.json>

Reads assertions (JSON array) from stdin, evaluates them against events
extracted from OTEL trace spans, and prints results as a JSON array to stdout.

The trace file contains one JSON object per line (JSONL), each representing
an OTEL span. Events are extracted from span Events arrays and flattened
into dicts with attribute keys as fields.

Assertion types:
  no_event  - FAIL if any matching event exists
  has_event - FAIL if no matching event exists
  info      - Extract a value from matching events (never fails)
"""

import json
import sys


def extract_attr_value(attr_value):
    """Extract the plain value from an OTEL attribute Value object."""
    if isinstance(attr_value, dict):
        # OTEL SDK format: {"Type": "STRING", "Value": "foo"}
        # or nested: {"Type": "STRINGSLICE", "Value": [...]}
        return attr_value.get("Value", attr_value)
    return attr_value


def flatten_attributes(attrs):
    """Convert OTEL Attributes list to a flat dict."""
    result = {}
    if not attrs:
        return result
    for attr in attrs:
        key = attr.get("Key", "")
        value = extract_attr_value(attr.get("Value", {}))
        result[key] = value
    return result


def load_events(path):
    """Load events from OTEL trace JSONL file.

    Extracts events from all spans and flattens them into dicts
    compatible with the assertion matching format:
      {"type": <event_name>, "elapsed_ms": ..., <attr_key>: <attr_value>, ...}
    """
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                span = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Extract events from the span
            span_events = span.get("Events", [])
            if not span_events:
                continue

            for evt in span_events:
                flat = flatten_attributes(evt.get("Attributes", []))
                flat["type"] = evt.get("Name", "")
                flat["_span_name"] = span.get("Name", "")
                flat["_time"] = evt.get("Time", "")
                # Convert stringified numbers
                if "elapsed_ms" in flat:
                    try:
                        flat["elapsed_ms"] = int(flat["elapsed_ms"])
                    except (ValueError, TypeError):
                        pass
                events.append(flat)

    return events


def matches(event, event_type, filters):
    # Check event type
    etype = event.get("type", "")
    if etype != event_type:
        return False
    if not filters:
        return True
    for key, val in filters.items():
        if key == "address_contains":
            addrs = json.dumps(event.get("addresses", ""))
            if val not in addrs:
                return False
        elif key == "message_contains":
            if val not in json.dumps(event):
                return False
        elif key == "reachability":
            if event.get("reachability") != val:
                return False
        elif key == "not_empty":
            # val is a field name; passes only if that field is a non-empty list
            field_val = event.get(val, [])
            if not isinstance(field_val, list) or len(field_val) == 0:
                return False
        elif key == "is_empty":
            # val is a field name; passes only if that field is an empty list
            field_val = event.get(val, [])
            if not isinstance(field_val, list) or len(field_val) > 0:
                return False
        else:
            if str(event.get(key, "")) != str(val):
                return False
    return True


def evaluate(assertion, events):
    atype = assertion["type"]
    event_type = assertion.get("event", "")
    filters = assertion.get("filter", {})
    message = assertion.get("message", "")
    matched = [e for e in events if matches(e, event_type, filters)]

    if atype == "no_event":
        ok = len(matched) == 0
        return {
            "type": atype,
            "pass": ok,
            "status": "PASS" if ok else "FAIL",
            "message": message,
            "matched_count": len(matched),
        }

    if atype == "has_event":
        ok = len(matched) > 0
        return {
            "type": atype,
            "pass": ok,
            "status": "PASS" if ok else "FAIL",
            "message": message,
            "matched_count": len(matched),
        }

    if atype == "info":
        field = assertion.get("extract", "")
        select_ = assertion.get("select", "first")
        label = assertion.get("label", message)
        value = None
        if matched:
            target = matched[0] if select_ == "first" else matched[-1]
            value = target.get(field)
        return {
            "type": atype,
            "pass": True,
            "status": "INFO",
            "label": label,
            "value": value,
        }

    return {"type": atype, "pass": False, "status": "ERROR", "message": f"unknown type: {atype}"}


def main():
    if len(sys.argv) < 2:
        print("Usage: echo '<assertions_json>' | python3 eval-assertions.py <trace.json>", file=sys.stderr)
        sys.exit(1)

    trace_path = sys.argv[1]
    assertions = json.loads(sys.stdin.read())
    events = load_events(trace_path)
    results = [evaluate(a, events) for a in assertions]
    json.dump(results, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
