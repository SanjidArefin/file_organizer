"""JSON-lines entry point for the File Organizer sidecar."""

from __future__ import annotations

import json
import sys
from typing import Any

import organizer_logic


def write_message(message: dict[str, Any]) -> None:
    # ASCII escapes keep the JSONL protocol independent of Windows console code pages.
    sys.stdout.write(json.dumps(message, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()


def write_progress(
    request_id: Any,
    action: str,
    phase: str,
    details: dict[str, Any] | None = None,
) -> None:
    message = {"event": "progress", "id": request_id, "action": action, "phase": phase}
    if details:
        message.update({key: value for key, value in details.items() if key != "event"})
    write_message(message)


def handle_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        return {
            "id": None,
            "ok": False,
            "error": {"code": "invalid_request", "message": "Request must be a JSON object."},
        }

    request_id = request.get("id")
    action = request.get("action")
    payload = request.get("payload", {})

    if not isinstance(action, str) or action not in organizer_logic.SUPPORTED_ACTIONS:
        return {
            "id": request_id,
            "ok": False,
            "error": {
                "code": "unknown_action",
                "message": "Action must be one of: scan, organize, confirm, start_session, undo.",
            },
        }
    if not isinstance(payload, dict):
        return {
            "id": request_id,
            "action": action,
            "ok": False,
            "error": {"code": "invalid_payload", "message": "Payload must be a JSON object."},
        }

    labels = {
        "scan": "Scanning",
        "organize": "Organizing",
        "confirm": "Saving review",
        "start_session": "Checking undo history",
        "undo": "Undoing changes",
    }
    if action != "start_session":
        write_progress(
            request_id,
            action,
            "started",
            {"label": labels[action], "message": f"Starting {action}."},
        )
    try:
        result = organizer_logic.handle_action(
            action,
            payload,
            progress=(
                lambda details: write_progress(request_id, action, "working", details)
            ) if action != "start_session" else None,
        )
    except Exception as error:  # Keep one bad request from ending the sidecar loop.
        if action != "start_session":
            write_progress(
                request_id,
                action,
                "failed",
                {"label": "Failed", "message": str(error)},
            )
        return {
            "id": request_id,
            "action": action,
            "ok": False,
            "error": {"code": "action_failed", "message": str(error)},
        }

    if action != "start_session":
        write_progress(
            request_id,
            action,
            "completed",
            {
                "label": "Complete",
                "message": f"{action.title()} request complete.",
                "subfolderIndex": 1,
                "subfolderTotal": 1,
                "fileIndex": 1,
                "fileTotal": 1,
                "indeterminate": False,
            },
        )
    return {"id": request_id, "action": action, "ok": True, "result": result}


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as error:
            write_message(
                {
                    "id": None,
                    "ok": False,
                    "error": {"code": "invalid_json", "message": str(error)},
                }
            )
            continue

        write_message(handle_request(request))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
