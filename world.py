#!/usr/bin/env python3
"""
Fetch JanusFi daily metrics (volume, traders, trades, oracle cost)
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
    """Format volume as $X.XXm / $X.XXk / $X.XX."""
    value = float(value or 0)
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}m"
    elif value >= 1_000:
        return f"${value / 1_000:.2f}k"
    return f"${value:.2f}"


def format_num(value):
    """Format a count as X.XXm / X.Xk / raw."""
    value = float(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}m"
    elif value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{int(value)}"


def format_sol(value):
    """Format oracle cost in SOL to 4 decimals."""
    return f"{float(value or 0):.4f}"


def format_change(pct_change):
    """Emoji + signed percent. Handles None as n/a."""
    if pct_change is None:
        return "  `n/a`"
    pct = float(pct_change)
    if pct >= 0:
        return f"  📈 `+{abs(pct):.1f}%`"
    return f"  📉 `-{abs(pct):.1f}%`"


def metric_row(label, val_y, val_d, change_str):
    """One backtick row: label + yesterday + day-before, then change."""
    name = label[:10].ljust(10)
    v1 = val_y.rjust(11)
    v2 = val_d.rjust(11)
    return f"`{name} {v1} {v2}`" + change_str


def build_blocks(tv, tr, orc):
    today = datetime.now(timezone.utc).date()
    y_str = (today - timedelta(days=1)).strftime("%b %d")
    d_str = (today - timedelta(days=2)).strftime("%b %d")

    header = f"`{'':<10} {y_str:>11} {d_str:>11}   Chg`"
    sep = "`" + "─" * 45 + "`"

    # ── Volume ──
    vol_text = "\n".join([
        header, sep,
        metric_row(
            "Volume",
            format_volume(tv.get("volume_yesterday")),
            format_volume(tv.get("volume_day_before")),
            format_change(tv.get("volume_pct_change")),
        ),
    ])

    # ── Traders (active + new) ──
    tr_lines = [
        header, sep,
        f"`{'Active':<10} "
        f"{format_num(tr.get('active_yesterday')):>11} "
        f"{format_num(tr.get('active_day_before')):>11}`",
    ]
    new_line = f"`{'New signers':<14} {format_num(tr.get('new_signers_yesterday')):>10}`"
    tr_text = "\n".join(tr_lines) + "\n" + new_line

    # ── Trades ──
    trd_text = "\n".join([
        header, sep,
        metric_row(
            "Trades",
            format_num(tv.get("trades_yesterday")),
            format_num(tv.get("trades_day_before")),
            format_change(tv.get("trades_pct_change")),
        ),
    ])

    # ── Oracle cost ──
    orc_text = "\n".join([
        header, sep,
        metric_row(
            "Cost ◎",
            format_sol(orc.get("oracle_cost_yesterday")),
            format_sol(orc.get("oracle_cost_day_before")),
            format_change(orc.get("oracle_cost_pct_change")),
        ),
    ])

    yesterday = today - timedelta(days=1)
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*JanusFi Daily Metrics*\n_{yesterday.strftime('%A, %B %d, %Y')}_",
            },
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "💰 *Volume*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": vol_text}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "👥 *Traders*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": tr_text}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "🔁 *Trades*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": trd_text}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "🔮 *Oracle Cost*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": orc_text}},
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "via Dune Analytics"}],
        },
    ]
    return blocks


def send_to_slack(tv, tr, orc):
    blocks = build_blocks(tv, tr, orc)
    payload = {"text": "JanusFi Daily Metrics", "blocks": blocks}
    response = requests.post(SLACK_WEBHOOK_URL, json=payload)
    if response.status_code == 200:
        print("Report sent to Slack successfully!")
        return True
    print(f"Failed to send to Slack: {response.status_code} - {response.text}")
    return False


def main():
    print(f"Starting JanusFi report at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    tv = fetch_dune_row(TRADES_VOL_QUERY_ID)
    tr = fetch_dune_row(TRADERS_QUERY_ID)
    orc = fetch_dune_row(ORACLE_QUERY_ID)

    if not (tv or tr or orc):
        print("No data fetched from Dune. Exiting.")
        return

    print("\nVolume:")
    print(f"  {format_volume(tv.get('volume_yesterday'))} vs {format_volume(tv.get('volume_day_before'))} ({tv.get('volume_pct_change')}%)")

    print("\nTraders:")
    print(f"  active: {format_num(tr.get('active_yesterday'))} vs {format_num(tr.get('active_day_before'))}")
    print(f"  new signers: {format_num(tr.get('new_signers_yesterday'))}")

    print("\nTrades:")
    print(f"  {format_num(tv.get('trades_yesterday'))} vs {format_num(tv.get('trades_day_before'))} ({tv.get('trades_pct_change')}%)")

    print("\nOracle Cost:")
    print(f"  {format_sol(orc.get('oracle_cost_yesterday'))} vs {format_sol(orc.get('oracle_cost_day_before'))} ({orc.get('oracle_cost_pct_change')}%)")

    send_to_slack(tv, tr, orc)
    print("\nJob completed!")


if __name__ == "__main__":
    main()
