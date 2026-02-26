#!/usr/bin/env python3
"""Evaluate assertions against JSONL experiment logs.

Usage: echo '<assertions_json>' | python3 eval-assertions.py <logfile.jsonl>

Reads assertions (JSON array) from stdin, evaluates them against the JSONL log
file, and prints results as a JSON array to stdout.

Assertion types:
  no_event  - FAIL if any matching event exists
  has_event - FAIL if no matching event exists
  info      - Extract a value from matching events (never fails)
"""

import json
import sys


def load_events(path):
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


def matches(event, event_type, filters):
    # Check event type — match against "type" or "event" field
    etype = event.get("type") or event.get("event", "")
    if etype != event_type:
        return False
    if not filters:
        return True
    for key, val in filters.items():
        if key == "address_contains":
            addrs = json.dumps(event.get("addresses", event.get("address", "")))
            if val not in addrs:
                return False
        elif key == "message_contains":
            if val not in json.dumps(event):
                return False
        elif key == "reachability":
            if event.get("reachability") != val:
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
        select = assertion.get("select", "first")
        label = assertion.get("label", message)
        value = None
        if matched:
            target = matched[0] if select == "first" else matched[-1]
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
        print("Usage: echo '<assertions_json>' | python3 eval-assertions.py <logfile>", file=sys.stderr)
        sys.exit(1)

    log_path = sys.argv[1]
    assertions = json.loads(sys.stdin.read())
    events = load_events(log_path)
    results = [evaluate(a, events) for a in assertions]
    json.dump(results, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
