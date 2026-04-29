#!/usr/bin/env python3
"""Update qTest Test Run custom fields for every run nested under a Test Cycle.

Used by the Jenkins pipeline after a Tosca TestEvent has run and Tosca's
native qTest integration has synced results into the matching Test Cycle.
"""

import argparse
import json
import sys
import time
from typing import Any

import requests


def parse_field(arg: str) -> dict[str, Any]:
    if "=" not in arg:
        raise argparse.ArgumentTypeError(
            f"--field expects 'fieldId=value', got: {arg!r}"
        )
    field_id, value = arg.split("=", 1)
    field_id = field_id.strip()
    if not field_id.isdigit():
        raise argparse.ArgumentTypeError(
            f"--field id must be numeric, got: {field_id!r}"
        )
    return {"field_id": int(field_id), "field_value": value}


def pick_auth_header(
    session: requests.Session, base: str, project_id: int, raw_token: str
) -> str:
    """Probe GET /projects/{pid} with each plausible Authorization format
    and return the first one that returns 2xx. qTest tokens come in two
    common shapes: a raw value used as `Authorization: <token>`, and a
    bearer-style value used as `Authorization: Bearer <token>`. If the
    user pasted the token with a 'Bearer '/'bearer ' prefix already,
    use it verbatim.
    """
    token = raw_token.strip()
    if token.lower().startswith("bearer "):
        candidates = [token]
    else:
        candidates = [f"Bearer {token}", token]
    probe_url = f"{base}/api/v3/projects/{project_id}"
    last_status = None
    last_body = ""
    for value in candidates:
        r = session.get(probe_url, headers={"Authorization": value}, timeout=30)
        if r.ok:
            print(
                f"qTest auth OK using header style: "
                f"{'Bearer <token>' if value.lower().startswith('bearer ') else '<token> (raw)'}"
            )
            return value
        last_status = r.status_code
        last_body = r.text[:300]
    raise SystemExit(
        f"qTest auth probe failed: HTTP {last_status} body={last_body}. "
        "Verify the qtest-pat Jenkins credential holds a valid Personal "
        "Access Token for sademo.qtestnet.com with access to project "
        f"{project_id}."
    )


def find_cycle_id(
    session: requests.Session, base: str, project_id: int, name: str
) -> int:
    url = f"{base}/api/v3/projects/{project_id}/search"
    body = {
        "object_type": "test-cycles",
        "fields": ["*"],
        "query": f"Name = '{name}'",
    }
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            r = session.post(url, json=body, timeout=30)
            r.raise_for_status()
            data = r.json()
            items = data.get("items") or data.get("data") or []
            if items:
                if len(items) > 1:
                    print(
                        f"WARN: {len(items)} cycles named {name!r}; "
                        "picking most recently modified",
                        file=sys.stderr,
                    )
                    items.sort(
                        key=lambda x: x.get("last_modified_date") or "",
                        reverse=True,
                    )
                cycle = items[0]
                cycle_id = cycle.get("id") or cycle.get("pid")
                if isinstance(cycle_id, str) and cycle_id.startswith("CL-"):
                    cycle_id = cycle.get("id")
                if cycle_id:
                    return int(cycle_id)
            print(
                f"Attempt {attempt}/3: cycle {name!r} not found yet, "
                "retrying in 5s...",
                file=sys.stderr,
            )
        except requests.HTTPError as e:
            last_err = e
            print(
                f"Attempt {attempt}/3: search failed: {e} "
                f"body={getattr(e.response, 'text', '')[:300]}",
                file=sys.stderr,
            )
        if attempt < 3:
            time.sleep(5)
    if last_err:
        raise last_err
    raise SystemExit(f"Test Cycle named {name!r} not found after 3 attempts.")


def list_descendant_runs(
    session: requests.Session, base: str, project_id: int, cycle_id: int
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    page = 1
    page_size = 200
    while True:
        params = {
            "parentId": cycle_id,
            "parentType": "test-cycle",
            "expand": "descendants",
            "page": page,
            "pageSize": page_size,
        }
        r = session.get(
            f"{base}/api/v3/projects/{project_id}/test-runs",
            params=params,
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("items") if isinstance(data, dict) else data
        if not items:
            break
        runs.extend(items)
        if len(items) < page_size:
            break
        page += 1
    return runs


def update_run(
    session: requests.Session,
    base: str,
    project_id: int,
    run: dict[str, Any],
    field_updates: list[dict[str, Any]],
) -> None:
    run_id = run["id"]
    body = {
        "name": run.get("name", ""),
        "properties": field_updates,
    }
    r = session.put(
        f"{base}/api/v3/projects/{project_id}/test-runs/{run_id}",
        json=body,
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(
            f"PUT run {run_id} failed: HTTP {r.status_code} {r.text[:300]}"
        )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--project-id", required=True, type=int)
    p.add_argument("--token", required=True)
    p.add_argument("--cycle-name", required=True)
    p.add_argument(
        "--field",
        action="append",
        required=True,
        type=parse_field,
        help="Repeatable. Form: fieldId=value",
    )
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    session = requests.Session()
    session.headers.update(
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    auth_value = pick_auth_header(session, base, args.project_id, args.token)
    session.headers["Authorization"] = auth_value

    cycle_id = find_cycle_id(session, base, args.project_id, args.cycle_name)
    print(f"Found Test Cycle '{args.cycle_name}' id={cycle_id}")

    runs = list_descendant_runs(session, base, args.project_id, cycle_id)
    if not runs:
        print(
            f"ERROR: no Test Runs found under cycle id={cycle_id}",
            file=sys.stderr,
        )
        return 1

    print(f"Found {len(runs)} descendant Test Run(s); updating...")
    print(
        "Updates: "
        + json.dumps(args.field, separators=(",", ":")),
    )

    ok = 0
    failures: list[str] = []
    for run in runs:
        try:
            update_run(session, base, args.project_id, run, args.field)
            ok += 1
        except Exception as e:
            failures.append(f"  - run {run.get('id')} ({run.get('name')!r}): {e}")

    print(
        f"Updated {ok} of {len(runs)} test runs under cycle "
        f"'{args.cycle_name}' (id={cycle_id})"
    )
    if failures:
        print("Failures:", file=sys.stderr)
        for line in failures:
            print(line, file=sys.stderr)

    return 0 if ok == len(runs) else 1


if __name__ == "__main__":
    sys.exit(main())
