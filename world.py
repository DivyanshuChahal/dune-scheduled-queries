#!/usr/bin/env python3
"""
Fetch World daily metrics (volume, traders, trades, oracle cost)
from three single-row Dune queries and post a compact report to Slack.
"""

import os
import requests
from datetime import datetime, timezone, timedelta
from dune_client.client import DuneClient
from dotenv import load_dotenv

load_dotenv()

DUNE_API_KEY = os.getenv("DUNE_API_KEY_FRONTEND")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_JANUS")

TRADES_VOL_QUERY_ID = 7838619   # trades_* , volume_*
TRADERS_QUERY_ID    = 7838557   # active_* , new_signers_yesterday
ORACLE_QUERY_ID     = 7838666   # oracle_cost_*

if not DUNE_API_KEY:
    raise SystemExit("DUNE_API_KEY_FRONTEND is not set — add it as a repo secret.")
if not SLACK_WEBHOOK_URL:
    raise SystemExit("SLACK_WEBHOOK_JANUS is not set — add it as a repo secret.")


def fetch_dune_row(query_id):
    """Fetch a single-row Dune result; return {} if empty."""
    print(f"Fetching Dune query {query_id}...")
    dune = DuneClient(DUNE_API_KEY)
    try:
        query_result = dune.get_latest_result(query_id)
    except Exception as e:
        print(f"No cached results, executing... ({e})")
        from dune_client.query import QueryBase
        query_result = dune.run_query(QueryBase(query_id=query_id))

    rows = query_result.result.rows
    print(f"Fetched {len(rows)} rows")
    return rows[0] if rows else {}


def format_volume(value):
    value = float(value or 0)
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}m"
    elif value >= 1_000:
        return f"${value / 1_000:.2f}k"
    return f"${value:.2f}"


def format_num(value):
    value = float(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}m"
    elif value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{int(value)}"


def format_sol(value):
    return f"{float(value or 0):.4f}"


def pct_change(new, old):
    """Percent change, or None if there's no baseline."""
    new = float(new or 0)
    old = float(old or 0)
    if old == 0:
        return None
    return (new - old) / old * 100


def format_change(pct):
    """Emoji + signed percent; '' when pct is None/empty (no baseline).
    Coerces to float since Dune returns ROUND() columns as strings."""
    if pct is None or pct == "":
        return ""
    pct = float(pct)
    if pct >= 0:
        return f"  📈 `+{abs(pct):.1f}%`"
    return f"  📉 `-{abs(pct):.1f}%`"


# monospace column widths
LBL, COL1, COL2 = 12, 11, 12


def data_row(label, val_y, val_d, change):
    cell = f"{label:<{LBL}}{val_y:>{COL1}}{val_d:>{COL2}}"
    return f"`{cell}`" + format_change(change)


def build_blocks(tv, tr, orc):
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    y_str = yesterday.strftime("%b %d")
    d_str = (today - timedelta(days=2)).strftime("%b %d")

    chg_label = "% Change"
    header = f"`{'Metric':<{LBL}}{y_str:>{COL1}}{d_str:>{COL2}}   {chg_label}`"
    sep = "`" + "─" * (LBL + COL1 + COL2 + 3 + len(chg_label)) + "`"

    rows = [
        header,
        sep,
        data_row(
            "Volume",
            format_volume(tv.get("volume_yesterday")),
            format_volume(tv.get("volume_day_before")),
            tv.get("volume_pct_change"),
        ),
        data_row(
            "Traders",
            format_num(tr.get("active_yesterday")),
            format_num(tr.get("active_day_before")),
            pct_change(tr.get("active_yesterday"), tr.get("active_day_before")),
        ),
        # new traders has no prior-day baseline -> dash, no change arrow
        f"`{'New traders':<{LBL}}{format_num(tr.get('new_signers_yesterday')):>{COL1}}{'—':>{COL2}}`",
        data_row(
            "Trades",
            format_num(tv.get("trades_yesterday")),
            format_num(tv.get("trades_day_before")),
            tv.get("trades_pct_change"),
        ),
        data_row(
            "Oracle Cost",
            format_sol(orc.get("oracle_cost_yesterday")),
            format_sol(orc.get("oracle_cost_day_before")),
            orc.get("oracle_cost_pct_change"),
        ),
    ]
    table = "\n".join(rows)

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*World Daily Metrics*\n_{yesterday.strftime('%A, %B %d, %Y')}_",
            },
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": table}},
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "via Dune Analytics"}],
        },
    ]


def send_to_slack(tv, tr, orc):
    blocks = build_blocks(tv, tr, orc)
    payload = {"text": "World Daily Metrics", "blocks": blocks}
    response = requests.post(SLACK_WEBHOOK_URL, json=payload)
    if response.status_code == 200:
        print("Report sent to Slack successfully!")
        return True
    print(f"Failed to send to Slack: {response.status_code} - {response.text}")
    return False


def main():
    print(f"Starting World report at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    tv = fetch_dune_row(TRADES_VOL_QUERY_ID)
    tr = fetch_dune_row(TRADERS_QUERY_ID)
    orc = fetch_dune_row(ORACLE_QUERY_ID)

    if not (tv or tr or orc):
        print("No data fetched from Dune. Exiting.")
        return

    print("\nVolume:", format_volume(tv.get("volume_yesterday")), "vs", format_volume(tv.get("volume_day_before")))
    print("Traders:", format_num(tr.get("active_yesterday")), "vs", format_num(tr.get("active_day_before")))
    print("New traders:", format_num(tr.get("new_signers_yesterday")))
    print("Trades:", format_num(tv.get("trades_yesterday")), "vs", format_num(tv.get("trades_day_before")))
    print("Oracle:", format_sol(orc.get("oracle_cost_yesterday")), "vs", format_sol(orc.get("oracle_cost_day_before")))

    send_to_slack(tv, tr, orc)
    print("\nJob completed!")


if __name__ == "__main__":
    main()
