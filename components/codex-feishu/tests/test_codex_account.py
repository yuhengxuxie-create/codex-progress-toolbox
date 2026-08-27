from __future__ import annotations

from progress_wx.codex_account import (
    AccountRateLimits,
    CodexAccountReader,
    RateLimitWindow,
    format_rate_limits,
    parse_rate_limits_response,
)


def test_parse_official_rate_limits_and_reset_credits() -> None:
    snapshot = parse_rate_limits_response(
        {
            "result": {
                "rateLimitsByLimitId": {
                    "codex_bengalfox": {
                        "limitName": "GPT-5.3-Codex-Spark",
                        "primary": {
                            "usedPercent": 0,
                            "windowDurationMins": 300,
                            "resetsAt": 1_800_000_000,
                        },
                    },
                    "codex": {
                        "primary": {
                            "usedPercent": 25.5,
                            "windowDurationMins": 300,
                            "resetsAt": 1_800_000_000,
                        },
                        "secondary": {
                            "usedPercent": 75,
                            "windowDurationMins": 10080,
                            "resetsAt": 1_800_100_000,
                        },
                    }
                },
                "rateLimitResetCredits": {
                    "availableCount": 2,
                    "credits": [
                        {
                            "id": "credit-1",
                            "status": "available",
                            "title": "补偿卡",
                            "expiresAt": 1_900_000_000,
                        }
                    ],
                },
            }
        }
    )
    assert [item.window_name for item in snapshot.windows] == ["5 小时额度", "每周额度"]
    assert [item.available_percent for item in snapshot.windows] == [74.5, 25.0]
    assert snapshot.reset_credit_count == 2
    assert snapshot.reset_credits[0].expires_at == 1_900_000_000
    text = format_rate_limits(snapshot)
    assert text == "Codex 每周额度：25%\n剩余重置卡：2 张"
    assert "Spark" not in text


def test_no_reset_credit_uses_exact_user_requested_text() -> None:
    text = format_rate_limits(
        AccountRateLimits(
            (
                RateLimitWindow("codex", "Codex", "每周额度", 51, 49, 10080, 1_800_000_000),
            ),
            0,
            (),
        )
    )
    assert text == "Codex 每周额度：49%\n剩余重置卡：0 张"


def test_reader_closes_rpc_after_exact_official_method() -> None:
    class FakeRPC:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []
            self.closed = False

        def request(self, method: str, params=None, **_kwargs):
            self.calls.append((method, params))
            return {
                "result": {
                    "rateLimits": {
                        "primary": {
                            "usedPercent": 1,
                            "windowDurationMins": 300,
                            "resetsAt": 1_800_000_000,
                        }
                    },
                    "rateLimitResetCredits": {"availableCount": 0, "credits": []},
                }
            }

        def close(self) -> None:
            self.closed = True

    rpc = FakeRPC()
    result = CodexAccountReader("codex", rpc_factory=lambda: rpc).read()
    assert result.windows[0].available_percent == 99
    assert rpc.calls == [("account/rateLimits/read", None)]
    assert rpc.closed is True
