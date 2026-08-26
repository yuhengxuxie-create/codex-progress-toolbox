"""Tiny JSON-lines Codex App Server fixture for standard-library tests."""

from __future__ import annotations

import json
import sys


def reply(request_id: object, result: object) -> None:
    print(
        json.dumps({"id": request_id, "result": result}, ensure_ascii=True),
        flush=True,
    )


for raw_line in sys.stdin:
    try:
        message = json.loads(raw_line)
    except json.JSONDecodeError:
        continue
    method = message.get("method")
    if "id" not in message:
        continue
    request_id = message["id"]
    params = message.get("params", {})

    if method == "initialize":
        reply(request_id, {"userAgent": "fake-codex-app-server/1"})
    elif method == "thread/read":
        thread_id = params.get("threadId")
        reply(
            request_id,
            {
                "thread": {
                    "id": thread_id,
                    "name": f"标题-{thread_id}",
                    "turns": [] if params.get("includeTurns") else None,
                    "status": {"type": "notLoaded"},
                }
            },
        )
    elif method == "thread/list":
        cursor = params.get("cursor")
        if params.get("sourceKinds") == ["appServer"]:
            reply(
                request_id,
                {
                    "data": [
                        {
                            "id": "thr_project",
                            "name": "项目会话",
                            "source": "appServer",
                            "cwd": "/workspace/project",
                            "status": {"type": "idle"},
                        }
                    ],
                    "nextCursor": None,
                },
            )
        elif cursor:
            reply(
                request_id,
                {
                    "data": [
                        {"id": "thr_b", "name": "第二个", "status": {"type": "idle"}}
                    ],
                    "nextCursor": None,
                },
            )
        else:
            reply(
                request_id,
                {
                    "data": [
                        {"id": "thr_a", "name": "第一个", "status": {"type": "active"}}
                    ],
                    "nextCursor": "page-2",
                },
            )
    else:
        print(
            json.dumps(
                {
                    "id": request_id,
                    "error": {"code": -32601, "message": f"unknown method {method}"},
                }
            ),
            flush=True,
        )
