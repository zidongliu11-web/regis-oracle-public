"""Raw, source-bound collectors for market-cognition forward completion."""

from __future__ import annotations

import base64
import json
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import requests


SH_TZ = ZoneInfo("Asia/Shanghai")
SINA_PROVIDER = "Sina Finance public batch quote"
SINA_ENDPOINT = "https://hq.sinajs.cn/list="
CNINFO_PROVIDER = "CNINFO official corporate-action API"
CNINFO_ENDPOINT = "https://webapi.cninfo.com.cn/api/sysapi/p_sysapi1139"
BAOSTOCK_CALENDAR_PROVIDER = "BaoStock query_trade_dates"
BAOSTOCK_CALENDAR_ENDPOINT = (
    "http://baostock.com/baostock/index.php/Python_API%E6%96%87%E6%A1%A3"
)


def parse_sina_completed_close_envelope(raw: bytes) -> dict[str, Any]:
    envelope = json.loads(raw.decode("utf-8"))
    if not isinstance(envelope, dict) or envelope.get("schema") != (
        "sina_completed_close_raw_envelope.v1"
    ):
        raise ValueError("sina_close_envelope_invalid")
    codes = envelope.get("requested_codes")
    if (
        not isinstance(codes, list)
        or codes != sorted(set(str(code) for code in codes))
    ):
        raise ValueError("sina_close_requested_codes_invalid")
    trade_date = str(envelope.get("trade_date") or "")
    payload = envelope.get("response_body_base64")
    if not isinstance(payload, str):
        raise ValueError("sina_close_response_body_invalid")
    response = base64.b64decode(payload.encode("ascii"), validate=True)
    prices: dict[str, float] = {}
    exchange_times: list[datetime] = []
    for line in response.decode("gb18030", errors="strict").splitlines():
        if '="' not in line or "hq_str_" not in line:
            continue
        symbol = line.split("hq_str_", 1)[1].split("=", 1)[0]
        code = symbol[2:] if symbol.startswith(("sh", "sz")) else ""
        if code not in codes:
            continue
        fields = line.split('="', 1)[1].rsplit('"', 1)[0].split(",")
        if len(fields) < 32 or not fields[0] or code in prices:
            raise ValueError("sina_close_quote_row_invalid")
        try:
            price = float(fields[3])
            exchange_at = datetime.fromisoformat(
                f"{fields[30]}T{fields[31]}+08:00"
            ).astimezone(SH_TZ)
        except (TypeError, ValueError) as exc:
            raise ValueError("sina_close_quote_value_invalid") from exc
        if price <= 0 or exchange_at.date().isoformat() != trade_date:
            raise ValueError("sina_close_quote_chronology_invalid")
        prices[code] = price
        exchange_times.append(exchange_at)
    if codes and set(prices) != set(codes):
        raise ValueError("sina_close_quote_coverage_incomplete")
    if not codes:
        if response or envelope.get("request_skipped_empty_position_set") is not True:
            raise ValueError("sina_close_empty_position_evidence_invalid")
        source_as_of = datetime.fromisoformat(
            str(envelope.get("session_close_at") or "")
        ).astimezone(SH_TZ)
        if source_as_of.date().isoformat() != trade_date:
            raise ValueError("sina_close_empty_position_chronology_invalid")
    else:
        source_as_of = max(exchange_times)
    return {
        "provider": SINA_PROVIDER,
        "provider_endpoint": SINA_ENDPOINT,
        "source_as_of": source_as_of.isoformat(timespec="seconds"),
        "prices_yuan": prices,
    }


def parse_cninfo_corporate_action_envelope(raw: bytes) -> dict[str, Any]:
    envelope = json.loads(raw.decode("utf-8"))
    if not isinstance(envelope, dict) or envelope.get("schema") != (
        "cninfo_corporate_action_raw_envelope.v1"
    ):
        raise ValueError("cninfo_action_envelope_invalid")
    codes = envelope.get("requested_codes")
    responses = envelope.get("responses")
    if (
        not isinstance(codes, list)
        or codes != sorted(set(str(code) for code in codes))
        or not isinstance(responses, list)
    ):
        raise ValueError("cninfo_action_coverage_invalid")
    trade_date = str(envelope.get("trade_date") or "")
    observed_at = datetime.fromisoformat(
        str(envelope.get("observed_at") or "")
    ).astimezone(SH_TZ)
    if observed_at.date().isoformat() != trade_date:
        raise ValueError("cninfo_action_chronology_invalid")
    seen: set[str] = set()
    actions: list[dict[str, Any]] = []
    for response in responses:
        if not isinstance(response, dict):
            raise ValueError("cninfo_action_response_invalid")
        code = str(response.get("code") or "")
        if code not in codes or code in seen or response.get("status_code") != 200:
            raise ValueError("cninfo_action_response_identity_invalid")
        seen.add(code)
        body = base64.b64decode(
            str(response.get("body_base64") or "").encode("ascii"), validate=True,
        )
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict) or str(value.get("resultcode") or "") != "200":
            raise ValueError("cninfo_action_provider_result_invalid")
        records = value.get("records") or []
        if not isinstance(records, list):
            raise ValueError("cninfo_action_records_invalid")
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("cninfo_action_record_invalid")
            raw_ex_date = str(record.get("F020D") or "").strip()
            ex_date = raw_ex_date[:10] if len(raw_ex_date) >= 10 else ""
            if ex_date != trade_date:
                continue
            actions.append({
                "stock_code": code,
                "event_type": "dividend_or_share_distribution",
                "effective_date": ex_date,
                "record_date": str(record.get("F018D") or "")[:10] or None,
                "cash_dividend_per_10": record.get("F012N"),
                "bonus_shares_per_10": record.get("F010N"),
                "transfer_shares_per_10": record.get("F011N"),
                "announcement_date": str(record.get("F006D") or "")[:10] or None,
            })
    if seen != set(codes):
        raise ValueError("cninfo_action_coverage_incomplete")
    actions.sort(key=lambda row: (row["stock_code"], row["effective_date"]))
    return {
        "provider": CNINFO_PROVIDER,
        "provider_endpoint": CNINFO_ENDPOINT,
        "source_as_of": observed_at.isoformat(timespec="seconds"),
        "coverage_codes": codes,
        "coverage_complete": True,
        "actions": actions,
    }


def parse_baostock_calendar_snapshot(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or value.get("source") != BAOSTOCK_CALENDAR_PROVIDER:
        raise ValueError("baostock_calendar_snapshot_invalid")
    dates = value.get("trading_dates")
    if not isinstance(dates, list) or dates != sorted(set(dates)) or not dates:
        raise ValueError("baostock_calendar_dates_invalid")
    return {
        "provider": BAOSTOCK_CALENDAR_PROVIDER,
        "provider_endpoint": BAOSTOCK_CALENDAR_ENDPOINT,
        "coverage_start": value.get("coverage_start"),
        "coverage_end": value.get("coverage_end"),
        "trading_dates": dates,
    }


class SinaCompletedSessionPriceCollector:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False

    def capture_completed_session_prices(
        self, *, codes: list[str], trade_date: str, observed_at: datetime,
    ) -> dict[str, Any]:
        normalized = sorted(set(str(code) for code in codes))
        body = b""
        skipped = not normalized
        if normalized:
            symbols = [f"sh{code}" if code.startswith("6") else f"sz{code}" for code in normalized]
            response = self.session.get(
                SINA_ENDPOINT + ",".join(symbols),
                headers={
                    "Referer": "https://finance.sina.com.cn/",
                    "User-Agent": "Mozilla/5.0 A-Share-Trading-Assistant/1.0",
                },
                timeout=(3.05, 7.0),
            )
            response.raise_for_status()
            body = response.content
        envelope = {
            "schema": "sina_completed_close_raw_envelope.v1",
            "trade_date": trade_date,
            "observed_at": observed_at.astimezone(SH_TZ).isoformat(timespec="seconds"),
            "session_close_at": datetime.combine(
                observed_at.astimezone(SH_TZ).date(), time(15, 0), tzinfo=SH_TZ,
            ).isoformat(timespec="seconds"),
            "requested_codes": normalized,
            "request_skipped_empty_position_set": skipped,
            "response_body_base64": base64.b64encode(body).decode("ascii"),
        }
        raw = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        parsed = parse_sina_completed_close_envelope(raw)
        return {"raw_response_bytes": raw, "prices_yuan": parsed["prices_yuan"]}


class CNINFOCorporateActionCollector:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False

    @staticmethod
    def _headers() -> dict[str, str]:
        import py_mini_racer
        from akshare.stock.stock_dividend_cninfo import _get_file_content_ths

        javascript = py_mini_racer.MiniRacer()
        javascript.eval(_get_file_content_ths("cninfo.js"))
        return {
            "Accept": "*/*", "Accept-Enckey": javascript.call("getResCode1"),
            "Accept-Encoding": "gzip, deflate", "Accept-Language": "zh-CN,zh;q=0.9",
            "Cache-Control": "no-cache", "Content-Length": "0",
            "Host": "webapi.cninfo.com.cn", "Origin": "http://webapi.cninfo.com.cn",
            "Pragma": "no-cache", "Referer": "http://webapi.cninfo.com.cn/",
            "User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest",
        }

    def capture_corporate_actions(
        self, *, codes: list[str], trade_date: str, observed_at: datetime,
    ) -> dict[str, Any]:
        normalized = sorted(set(str(code) for code in codes))
        responses = []
        headers = self._headers() if normalized else {}
        for code in normalized:
            response = self.session.post(
                CNINFO_ENDPOINT, params={"scode": code}, headers=headers,
                timeout=(3.05, 10.0),
            )
            response.raise_for_status()
            responses.append({
                "code": code, "status_code": response.status_code,
                "body_base64": base64.b64encode(response.content).decode("ascii"),
            })
        envelope = {
            "schema": "cninfo_corporate_action_raw_envelope.v1",
            "trade_date": trade_date,
            "observed_at": observed_at.astimezone(SH_TZ).isoformat(timespec="seconds"),
            "requested_codes": normalized,
            "responses": responses,
        }
        raw = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        parsed = parse_cninfo_corporate_action_envelope(raw)
        return {"raw_response_bytes": raw, "actions": parsed["actions"]}


LIVE_PARSERS = {
    "sina_completed_close_envelope_v1": parse_sina_completed_close_envelope,
    "cninfo_corporate_action_envelope_v1": parse_cninfo_corporate_action_envelope,
    "baostock_calendar_snapshot_v1": parse_baostock_calendar_snapshot,
}


__all__ = [
    "BAOSTOCK_CALENDAR_ENDPOINT", "BAOSTOCK_CALENDAR_PROVIDER",
    "CNINFOCorporateActionCollector", "CNINFO_ENDPOINT", "CNINFO_PROVIDER",
    "LIVE_PARSERS", "SINA_ENDPOINT", "SINA_PROVIDER",
    "SinaCompletedSessionPriceCollector",
    "parse_baostock_calendar_snapshot",
    "parse_cninfo_corporate_action_envelope",
    "parse_sina_completed_close_envelope",
]
