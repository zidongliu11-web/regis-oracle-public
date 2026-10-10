"""One-shot worker for the optional official TradingAgents dependency.

It receives one JSON request on stdin and writes exactly one JSON response on
stdout so the web process can treat upstream crashes as a contained failure.
"""

from __future__ import annotations

import json


def main() -> int:
    print(json.dumps({
        "ok": False,
        "error": "外部付费 API 已禁用；TradingAgents 不会启动",
    }, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
