"""Read-only integrity inventory for a retired mainline research archive.

A matching SHA-256 only binds local bytes to a declaration. It neither
authenticates an external issue time nor recomputes historical performance.
No trade route imports this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


SCHEMA = "mainline_retired_archive_integrity.v1"
ARCHIVE = "research/STATE_CONDITIONED_MAINLINE_SWING_V1_TERMINAL_ARCHIVE.json"
ROUTE = "research/SYSTEM_ROUTE_JOINT_V2_FORWARD_DEPLOYMENT_ADMISSION_V42.json"
REFERENCES = {
    "run_admission": "research/STATE_CONDITIONED_MAINLINE_SWING_V1_RUN_ADMISSION.json",
    "blind_result": "research/STATE_CONDITIONED_MAINLINE_SWING_V1_BLIND_RESULT.json",
    "independent_result_audit": "research/STATE_CONDITIONED_MAINLINE_SWING_V1_BLIND_RESULT_AUDIT.json",
}
# Frozen hashes declared by state_conditioned_mainline_swing.py. Keep the
# retired selector unchanged; these bytes are an integrity inventory only.
PROTOCOL_REFERENCES = {
    "base": ("research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL.json",
             "990e0cdad9196d9be07de8d70c787e9125c794ead0b96df166121d5526ff77b3"),
    "amendment_1": ("research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL_AMENDMENT_1.json",
                    "2b9a853755043bb61927319868beb5e1d32420e49ed749978cd2082e4f2bd6fc"),
    "amendment_2": ("research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL_AMENDMENT_2.json",
                    "90ed8e30ae004aa57fa94b0547a136403ad4ea36d304b5db78dd1553093f6900"),
    "amendment_3": ("research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL_AMENDMENT_3.json",
                    "4ca8a7b13139a769965d5b84c49ceec9c45038d4467d39e14f04748296802bed"),
    "amendment_4": ("research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL_AMENDMENT_4.json",
                    "13e65de1754d99c12c38fe6c47fc4af2d2f3f0d014e65dc843cdd7c17fbba8cf"),
    "amendment_5": ("research/STATE_CONDITIONED_MAINLINE_SWING_V1_PROTOCOL_AMENDMENT_5.json",
                    "adcf7ba169c55fc1c03236c9c99ffaca61fd830b73db5dd18c139445169d4521"),
}
MAX_ARCHIVE_BYTES = 128 * 1024
MAX_ROUTE_BYTES = 512 * 1024
MAX_REFERENCE_BYTES = 32 * 1024 * 1024
MAX_PROTOCOL_BYTES = 128 * 1024


def _hex(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and not set(value) - set("0123456789abcdef"))


def _trusted(root: Path, relative: str) -> Path:
    """Only fixed repository-relative paths, with no symlinked component."""
    if not isinstance(relative, str) or relative.startswith(("/", "\\")):
        raise ValueError("path_not_repository_relative")
    pieces = relative.split("/")
    if any(piece in ("", ".", "..") or "\\" in piece or ":" in piece
           for piece in pieces):
        raise ValueError("path_component_invalid")
    if root.is_symlink():
        raise ValueError("root_symlink")
    path = root
    for piece in pieces:
        path = path / piece
        if path.is_symlink():
            raise ValueError("path_symlink")
    return path


def _read_json(root: Path, relative: str, limit: int) -> tuple[dict, bytes]:
    path = _trusted(root, relative)
    if not path.is_file():
        raise FileNotFoundError(relative)
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("file_oversized")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("json_object_required")
    return value, raw


def _digest_file(root: Path, relative: str, limit: int) -> str:
    path = _trusted(root, relative)
    if not path.is_file():
        raise FileNotFoundError(relative)
    digest = hashlib.sha256()
    consumed = 0
    with path.open("rb") as stream:
        while chunk := stream.read(min(64 * 1024, limit - consumed + 1)):
            consumed += len(chunk)
            if consumed > limit:
                raise ValueError("file_oversized")
            digest.update(chunk)
    return digest.hexdigest()


def _state(error: Exception) -> str:
    if isinstance(error, FileNotFoundError):
        return "missing"
    if isinstance(error, (json.JSONDecodeError, UnicodeError)):
        return "invalid_json"
    if isinstance(error, ValueError):
        return str(error) if str(error) in {
            "file_oversized", "path_symlink", "root_symlink",
            "path_component_invalid", "path_not_repository_relative",
            "json_object_required"} else "invalid"
    return "unreadable"


def _protocol_reproduction(root: Path) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    for name, (relative, expected) in PROTOCOL_REFERENCES.items():
        record: dict[str, Any] = {"path": relative, "expected_sha256": expected}
        try:
            observed = _digest_file(root, relative, MAX_PROTOCOL_BYTES)
            record["state"] = "matched" if observed == expected else "hash_mismatch"
            record["observed_sha256"] = observed
        except (OSError, ValueError, TypeError) as error:
            record["state"] = _state(error)
        files[name] = record
    states = [item["state"] for item in files.values()]
    status = ("frozen_protocol_bytes_match_no_replay" if all(state == "matched" for state in states)
              else "frozen_protocols_missing" if "missing" in states
              else "frozen_protocols_invalid")
    return {"schema": "retired_mainline_protocol_reproduction.v1", "status": status,
            "files": files, "strategy_replay_validated": False,
            "historical_performance_recomputed": False,
            "production_eligible": False}


def audit(root: Path) -> dict[str, Any]:
    root = Path(root)
    result: dict[str, Any] = {
        "schema": SCHEMA, "status": "archive_missing",
        "archive": ARCHIVE, "archive_sha256": None,
        "route_binding": "not_checked", "references": {},
        "historical_performance_recomputed": False,
        "production_eligible": False, "generates_alert": False,
        "external_issue_time_attested": False,
        "protocol_reproduction": _protocol_reproduction(root),
    }
    try:
        archive, archive_raw = _read_json(root, ARCHIVE, MAX_ARCHIVE_BYTES)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        result["status"] = "archive_" + _state(error)
        return result
    archive_sha = hashlib.sha256(archive_raw).hexdigest()
    result["archive_sha256"] = archive_sha
    bindings = archive.get("bindings")
    if (archive.get("schema_version") != 1
            or archive.get("candidate_id") != "state_conditioned_mainline_swing_v1"
            or archive.get("status") != "terminal_historical_failure_archived_no_retune_no_production"
            or not isinstance(bindings, dict)
            or set(bindings) != set(REFERENCES)):
        result["status"] = "archive_contract_invalid"
        return result
    try:
        route, _ = _read_json(root, ROUTE, MAX_ROUTE_BYTES)
        terminal = route.get("bindings", {}).get("terminal_mainline_archive", {})
        result["route_binding"] = (
            "matched" if terminal.get("path") == ARCHIVE
            and terminal.get("sha256") == archive_sha
            else "mismatch")
    except (OSError, ValueError, TypeError, AttributeError,
            json.JSONDecodeError) as error:
        result["route_binding"] = _state(error)
    for name, relative in REFERENCES.items():
        binding = bindings[name]
        if (not isinstance(binding, dict) or binding.get("path") != relative
                or not _hex(binding.get("sha256"))):
            result["references"][name] = {"path": relative, "state": "binding_invalid"}
            continue
        try:
            observed = _digest_file(root, relative, MAX_REFERENCE_BYTES)
            state = "matched" if observed == binding["sha256"] else "hash_mismatch"
        except (OSError, ValueError, TypeError) as error:
            state = _state(error)
        result["references"][name] = {"path": relative, "state": state}
    states = [item["state"] for item in result["references"].values()]
    if result["route_binding"] != "matched":
        result["status"] = "route_binding_unverified"
    elif all(state == "matched" for state in states):
        result["status"] = "bound_receipts_present_not_recomputed"
    elif any(state == "missing" for state in states):
        result["status"] = "referenced_receipts_missing"
    else:
        result["status"] = "referenced_receipts_invalid"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    report = audit(args.root)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "bound_receipts_present_not_recomputed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
