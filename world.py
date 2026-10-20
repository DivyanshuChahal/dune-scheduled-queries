#!/usr/bin/env python3
"""
World daily metrics for Slack, sent as one image.

1. Pulls 3 Dune queries: trades & volume, oracle cost, fee streams
2. Draws one PNG: charts/world_report.png
3. Pushes the PNG to the CHART_BRANCH branch of this repo (the branch is replaced every run)
4. Posts it to Slack with the incoming webhook, using a raw.githubusercontent.com link

Slack webhooks can't take files, only image links, and Slack must open the link without
logging in. So the repo that holds the image has to be public. If this repo is private,
set CHART_REPO to a public repo (plus CHART_REPO_TOKEN that can push to it).

Env vars:
  DUNE_API_KEY_FRONTEND     Dune API key
  SLACK_WEBHOOK_JANUS       Slack incoming webhook URL
  GITHUB_TOKEN              given by GitHub Actions (pass it in the yml), used to push the image
  CHART_REPO, CHART_REPO_TOKEN   optional: push the image to a different (public) repo

pip install dune-client python-dotenv requests matplotlib numpy
"""

import logging
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no screen on GitHub runners
import matplotlib.pyplot as plt
import numpy as np
import requests
from dotenv import load_dotenv
from dune_client.client import DuneClient
from matplotlib import font_manager
from matplotlib.colors import to_rgb
from matplotlib.patches import Circle, FancyBboxPatch

load_dotenv()
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)  # hide harmless "findfont" warnings

DUNE_API_KEY = os.getenv("DUNE_API_KEY_FRONTEND")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_JANUS")

TRADES_VOL_QUERY_ID = 7838619   # trades_* , volume_*
ORACLE_QUERY_ID     = 7838666   # oracle_cost_*
FEES_QUERY_ID       = 7842592   # one row per stream: dflow / phantom / janusfi_protocol

# Where the image lives (a branch of this repo, replaced every run)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
CHART_REPO = os.getenv("CHART_REPO", "").strip() or os.getenv("GITHUB_REPOSITORY", "").strip()
CHART_TOKEN = os.getenv("CHART_REPO_TOKEN", "").strip() or GITHUB_TOKEN
CHART_BRANCH = "world-report-charts"
OUT = Path("charts/world_report.png")

if not DUNE_API_KEY:
    raise SystemExit("DUNE_API_KEY_FRONTEND is not set — add it as a repo secret.")
if not SLACK_WEBHOOK_URL:
    raise SystemExit("SLACK_WEBHOOK_JANUS is not set — add it as a repo secret.")


# ============================== data ==============================
def fetch_dune_rows(query_id):
    """Fetch all rows of a Dune result; return [] if empty."""
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
    return rows


def fetch_dune_row(query_id):
    """Fetch a single-row Dune result; return {} if empty."""
    rows = fetch_dune_rows(query_id)
    return rows[0] if rows else {}


def f0(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def pct(a, b):
    return (a / b - 1) * 100 if b else None


def metric(row, now_key, prev_key, pct_key):
    """(yesterday, day before, % change). Uses the query's % change; works it out if the query has none.
    Dune returns ROUND() columns as strings, so everything goes through f0."""
    now, before = f0(row.get(now_key)), f0(row.get(prev_key))
    p = row.get(pct_key)
    return now, before, (f0(p) if p not in (None, "") else pct(now, before))


def build_data(tv, orc, fee):
    today = datetime.now(timezone.utc).date()
    day, prev = today - timedelta(days=1), today - timedelta(days=2)
    fees = lambda stream: metric(fee.get(stream, {}), "fees_yesterday", "fees_day_before", "fees_pct_change")
    return {
        "day": day.strftime("%b %d"), "prev": prev.strftime("%b %d"), "long_day": day.strftime("%A, %B %d, %Y"),
        "metrics": {
            "Volume": metric(tv, "volume_yesterday", "volume_day_before", "volume_pct_change"),
            "Trades": metric(tv, "trades_yesterday", "trades_day_before", "trades_pct_change"),
            "Oracle Cost": metric(orc, "oracle_cost_yesterday", "oracle_cost_day_before", "oracle_cost_pct_change"),
            "DFlow Fees": fees("dflow"),
            "Phantom Fees": fees("phantom"),
            "JanusFi P Fees": fees("janusfi_protocol"),
        },
    }


# ============================== look ==============================
DPI = 150
W, H = 18, 10  # inches; the page is drawn in inch coordinates

BG_DEEP, BG_GLOW, BG_TINT = "#070B16", "#17213F", "#1A1335"
CARD, EDGE, TRACK = "#0E1528", "#1F2B4A", "#1A2440"
TEXT, MUTED, FAINT = "#E9EDF7", "#95A1BD", "#5B6786"
UP, DOWN, ACCENT = "#5EE3A1", "#FF8098", "#8B7BFF"

ACTIVITY = {"Volume": ACCENT, "Trades": "#5ED3B3", "Oracle Cost": "#FFD36E"}
FEES = {"DFlow Fees": ACCENT, "Phantom Fees": "#F48FB8", "JanusFi P Fees": "#FFB86B"}
LOWER_IS_BETTER = {"Oracle Cost"}  # a drop here is good news, so it shows in green

FONT_DIR = Path(__file__).with_name("fonts")
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/barlow/Barlow-{}.ttf"


def load_font():
    """Use Barlow (downloaded once into ./fonts); fall back to DejaVu Sans if that fails."""
    FONT_DIR.mkdir(exist_ok=True)
    try:
        for style in ("Regular", "Medium", "SemiBold", "Bold"):
            f = FONT_DIR / f"Barlow-{style}.ttf"
            if not f.exists():
                urllib.request.urlretrieve(FONT_URL.format(style), f)
            font_manager.fontManager.addfont(str(f))
        return "Barlow"
    except Exception as e:
        print(f"Font download failed, using DejaVu Sans ({e})")
        return "DejaVu Sans"


def money(v):
    if v >= 1e6:
        return f"${v / 1e6:.2f}m"
    if v >= 1e3:
        return f"${v / 1e3:.2f}k"
    return f"${v:,.2f}"


def fmt(name, v):
    if name == "Trades":
        return f"{v / 1e6:.2f}m" if v >= 1e6 else f"{v / 1e3:.1f}k" if v >= 1e3 else f"{v:,.0f}"
    if name == "Oracle Cost":
        return f"{v:.4f} SOL"
    return money(v)


def sign(p):
    return "–" if p is None else ("+" if p >= 0 else "−") + f"{abs(p):.1f}%"


def good(name, p):
    return (p or 0) <= 0 if name in LOWER_IS_BETTER else (p or 0) >= 0


class Page:
    def __init__(self, font):
        plt.rcParams.update({"font.family": [font, "DejaVu Sans"], "text.parse_math": False})
        self.fig = plt.figure(figsize=(W, H), dpi=DPI)
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.axis("off")
        self.r = self.fig.canvas.get_renderer()

    def text(self, x, y, s, size=11, c=TEXT, weight="regular", ha="left", va="baseline", z=6, **kw):
        return self.ax.text(x, y, s, fontsize=size, color=c, fontweight=weight, ha=ha, va=va, zorder=z, **kw)

    def width(self, t):
        return t.get_window_extent(self.r).width / DPI

    def pill(self, x, y, label, is_good, size=9.5, ha="left"):
        c = UP if is_good else DOWN
        return self.text(x, y, label, size, c, "semibold", ha=ha, va="center",
                         bbox=dict(boxstyle="round,pad=0.3,rounding_size=0.75", fc=(*to_rgb(c), .14), ec="none"))

    def rbox(self, x, y, w, h, r, fc="none", ec="none", lw=1, z=1, alpha=1):
        p = FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}", fc=fc, ec=ec, lw=lw,
                           zorder=z, alpha=alpha)
        self.ax.add_patch(p)
        return p

    def bar(self, x0, x1, yc, h, c, z=3):
        """Rounded bar from x0 to x1 that fades in towards its tip."""
        lo, hi = min(x0, x1), max(x0, x1)
        if hi - lo < h:
            lo, hi = (hi - h, hi) if x1 < x0 else (lo, lo + h)
        clip = self.rbox(lo, yc - h / 2, hi - lo, h, h / 2, z=z)
        img = np.zeros((1, 256, 4))
        img[..., :3] = to_rgb(c)
        img[..., 3] = np.linspace(.12, 1, 256)[::-1] if x1 < x0 else np.linspace(.12, 1, 256)
        im = self.ax.imshow(img, extent=(lo, hi, yc - h / 2, yc + h / 2), aspect="auto",
                            interpolation="bilinear", zorder=z)
        im.set_clip_path(clip)

    def glow_dot(self, x, y, r, c, z=5):
        for k, a in ((3.2, .06), (2.2, .1), (1.5, .18)):
            self.ax.add_patch(Circle((x, y), r * k, fc=c, ec="none", alpha=a, zorder=z))
        self.ax.add_patch(Circle((x, y), r, fc=c, ec="none", zorder=z + 1))

    def background(self):
        yy, xx = np.mgrid[0:1:240j, 0:1:360j]
        glow = np.clip(1 - np.hypot(xx - .12, (yy - .98) * 1.3) / .95, 0, 1) ** 1.6
        tint = np.clip(1 - np.hypot(xx - .95, (yy - .05) * 1.2) / .8, 0, 1) ** 2
        deep = np.array(to_rgb(BG_DEEP))
        img = (deep + (np.array(to_rgb(BG_GLOW)) - deep) * glow[..., None]
               + (np.array(to_rgb(BG_TINT)) - deep) * tint[..., None])
        self.ax.imshow(np.clip(img, 0, 1), extent=(0, W, 0, H), origin="lower", aspect="auto", zorder=-10,
                       interpolation="bicubic")

    def card(self, x, y, w, h):
        self.rbox(x, y, w, h, .2, fc=CARD, ec=EDGE, lw=1, z=0, alpha=.88)

    def section_title(self, x, top, num, title, note, wrap):
        self.ax.add_patch(Circle((x + .45, top - .42), .16, fc=ACCENT, ec="none", zorder=5))
        self.text(x + .45, top - .425, str(num), 11, "#FFFFFF", "bold", ha="center", va="center")
        self.text(x + .72, top - .42, title, 17, TEXT, "semibold", va="center")
        self.text(x + .3, top - .74, textwrap.fill(note, wrap), 11.5, MUTED, va="top", linespacing=1.35)

    def save(self, path):
        self.ax.set_xlim(0, W)
        self.ax.set_ylim(0, H)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fig.savefig(path, dpi=DPI)
        plt.close(self.fig)
        from PIL import Image  # Pillow ships with matplotlib
        Image.open(path).convert("RGB").save(path, optimize=True)  # smaller file, no alpha channel
        return path


# ============================== page parts ==============================
def section_activity(p, d, x0, y0, w, h):
    """1. Volume, trades and oracle cost: today vs the day before."""
    m = d["metrics"]
    p.card(x0, y0, w, h)
    note = ", ".join(f"{n.lower()} {sign(m[n][2])}" for n in ACTIVITY) + "."
    p.section_title(x0, y0 + h, 1, "Market activity", note[0].upper() + note[1:], 140)
    col, top = (w - .4) / len(ACTIVITY), y0 + h
    for i, (name, c) in enumerate(ACTIVITY.items()):
        x = x0 + .2 + i * col
        today, prev, ch = m[name]
        if i:
            p.ax.plot([x, x], [y0 + .35, top - 1.15], color=TRACK, lw=1, zorder=1)
        p.glow_dot(x + .3, top - 1.42, .045, c)
        p.text(x + .42, top - 1.42, name, 11, MUTED, "medium", va="center")
        vt = p.text(x + .25, top - 1.95, fmt(name, today), 24, TEXT, "semibold", va="center")
        p.pill(x + .25 + p.width(vt) + .12, top - 1.95, sign(ch), good(name, ch), 9.5)
        p.text(x + .25, top - 2.35, f"was {fmt(name, prev)}" + ("   (lower is better)" if name in LOWER_IS_BETTER else ""),
               9.5, FAINT, va="center")
        big = max(today, prev) or 1  # both days on one scale
        for j, (lab, v, bar_c) in enumerate(((d["day"], today, c), (d["prev"], prev, FAINT))):
            y = top - 2.85 - j * .32
            p.text(x + .25, y, lab, 8.5, FAINT, va="center")
            b0, b1 = x + .85, x + col - .3
            p.rbox(b0, y - .06, b1 - b0, .12, .06, fc=TRACK, z=2)
            if v > 0:
                p.bar(b0, b0 + (b1 - b0) * v / big, y, .12, bar_c, z=3)


def section_fees(p, d, x0, y0, w, h):
    """2. The three fee streams, their total, and each one's share."""
    m = d["metrics"]
    p.card(x0, y0, w, h)
    tot, tot_p = sum(m[n][0] for n in FEES), sum(m[n][1] for n in FEES)
    ch = pct(tot, tot_p)
    top_n = max(FEES, key=lambda n: m[n][0])
    note = f"All fees {sign(ch)} to {money(tot)} while volume moved {sign(m['Volume'][2])}."
    if tot:
        note += f" {top_n.replace(' Fees', '')} brings {m[top_n][0] / tot * 100:.1f}% of all fees."
    p.section_title(x0, y0 + h, 2, "Fees", note, 140)
    top = y0 + h

    p.text(x0 + .3, top - 1.45, "All fees", 11, MUTED, "medium", va="center")
    vt = p.text(x0 + .3, top - 1.95, money(tot), 26, TEXT, "semibold", va="center")
    p.pill(x0 + .3 + p.width(vt) + .12, top - 1.95, sign(ch), (ch or 0) >= 0, 10)
    p.text(x0 + .3, top - 2.35, f"was {money(tot_p)}", 9.5, FAINT, va="center")

    # stacked bars: length = that day's total, parts = each fee stream
    L0, L1, big = x0 + 4.0, x0 + w - 1.25, max(tot, tot_p) or 1
    for j, (lab, idx, total) in enumerate(((d["day"], 0, tot), (d["prev"], 1, tot_p))):
        y, x = top - 1.6 - j * .55, L0
        p.text(L0 - .12, y, lab, 10, TEXT if not j else MUTED, "semibold" if not j else "regular", ha="right",
               va="center")
        for n, c in FEES.items():
            seg = (L1 - L0) * m[n][idx] / big
            if seg > 0:
                p.rbox(x + .012, y - .15, max(seg - .024, .01), .3, min(.08, max(seg / 2 - .012, .005)), fc=c, z=3,
                       alpha=1 if not j else .7)
                label = money(m[n][idx])
                if seg > len(label) * .075 + .1:
                    p.text(x + seg / 2, y, label, 9.5, "#0B1020", "semibold", ha="center", va="center", z=7)
            x += seg
        p.text(x + .12, y, money(total), 12, TEXT if not j else MUTED, "semibold", va="center")

    # one row per fee stream
    c_name, c_now, c_chg, c_was, c_share = x0 + .45, x0 + 3.6, x0 + 3.75, x0 + 5.0, x0 + w - .3
    yh = top - 2.85
    p.text(c_now, yh, d["day"], 9, FAINT, ha="right", va="center")
    p.text(c_was, yh, d["prev"], 9, FAINT, va="center")
    p.text(c_share, yh, f"share of all fees ({d['prev']} → {d['day']})", 9, FAINT, ha="right", va="center")
    for i, (n, c) in enumerate(FEES.items()):
        y = yh - .4 - i * .4
        today, prev, chg_ = m[n]
        p.ax.plot([x0 + .3, x0 + w - .3], [y + .2, y + .2], color=TRACK, lw=1, zorder=1)
        p.glow_dot(c_name, y, .045, c)
        p.text(c_name + .14, y, n, 12, TEXT, "semibold", va="center")
        p.text(c_now, y, money(today), 12, TEXT, "semibold", ha="right", va="center")
        p.pill(c_chg, y, sign(chg_), (chg_ or 0) >= 0, 9)
        p.text(c_was, y, money(prev), 11, MUTED, va="center")
        s_prev = f"{prev / tot_p * 100:.1f}%" if tot_p else "–"
        s_now = f"{today / tot * 100:.1f}%" if tot else "–"
        p.text(c_share, y, f"{s_prev}  →  {s_now}", 11, TEXT, "semibold", ha="right", va="center")


def section_moved(p, d, x0, y0, w, h):
    """3. Every metric's % change, biggest rise first."""
    m = d["metrics"]
    p.card(x0, y0, w, h)
    rows = sorted(m, key=lambda n: -(m[n][2] or 0))
    p.section_title(x0, y0 + h, 3, "What moved", f"Change vs {d['prev']}, biggest rise first. "
                    "Green = good news (for oracle cost, a drop is good).", 62)
    zero, half = x0 + w / 2 + .1, w / 2 - 1.0
    biggest = max(abs(m[n][2] or 0) for n in rows) or 1
    first, last = y0 + h - 1.6, y0 + .5
    pitch = (first - last) / max(1, len(rows) - 1)
    p.ax.plot([zero, zero], [last - .3, first + .22], color=FAINT, lw=1, alpha=.6, zorder=2)
    for i, n in enumerate(rows):
        y, (today, prev, ch) = first - i * pitch, m[n]
        c = UP if good(n, ch) else DOWN
        p.text(x0 + .3, y + .2, n, 12, TEXT, "semibold", va="center")
        p.text(x0 + w - .3, y + .2, f"{fmt(n, prev)}  →  {fmt(n, today)}", 10, MUTED, ha="right", va="center")
        p.rbox(x0 + .3, y - .22, w - .6, .12, .06, fc=TRACK, z=1, alpha=.5)
        if ch is not None:
            tip = zero + half * ch / biggest
            p.bar(zero, tip, y - .16, .2, c)
            p.text(tip + (.1 if ch >= 0 else -.1), y - .16, sign(ch), 11, c, "bold",
                   ha="left" if ch >= 0 else "right", va="center")


def render(d, path=OUT):
    p = Page(load_font())
    p.background()
    day = p.text(.35, 9.42, d["day"], 27, TEXT, "bold")
    vs = p.text(.35 + p.width(day) + .22, 9.42, f"vs  {d['prev']}", 27, MUTED, "medium")
    p.text(vs.get_position()[0] + p.width(vs) + .35, 9.45, "World Daily Metrics", 14, FAINT, "medium")
    section_activity(p, d, .3, 5.15, 11.0, 3.85)
    section_fees(p, d, .3, .45, 11.0, 4.5)
    section_moved(p, d, 11.5, .45, 6.2, 8.55)
    p.text(.35, .2, f"All changes are vs {d['prev']}. In each activity tile, both days use the same scale.",
           9, FAINT, va="center")
    return p.save(path)


# ============================== publish & slack ==============================
def git(args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        if CHART_TOKEN:
            msg = msg.replace(CHART_TOKEN, "***")  # never print the token
        raise RuntimeError(f"git {args[0]} failed: {msg[:300]}")
    return r.stdout.strip()


def publish_to_github(path, d):
    """Put today's PNG on CHART_BRANCH as its only commit (force push, so old images are replaced).
    The link uses the commit id, so every day gets a new link and Slack never shows a cached old image."""
    work = tempfile.mkdtemp()
    shutil.copy(path, work)
    git(["init", "-q"], work)
    git(["symbolic-ref", "HEAD", f"refs/heads/{CHART_BRANCH}"], work)
    git(["config", "user.name", "github-actions[bot]"], work)
    git(["config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], work)
    git(["add", "-A"], work)
    git(["commit", "-q", "-m", f"world report for {d['day']}"], work)
    remote = f"https://x-access-token:{CHART_TOKEN}@github.com/{CHART_REPO}.git"
    git(["push", "-q", "--force", remote, f"HEAD:refs/heads/{CHART_BRANCH}"], work)
    sha = git(["rev-parse", "HEAD"], work)
    shutil.rmtree(work, ignore_errors=True)
    print(f"Pushed image to {CHART_REPO}, branch {CHART_BRANCH} ({sha[:7]})")
    return f"https://raw.githubusercontent.com/{CHART_REPO}/{sha}/{path.name}"


def link_works(url):
    """Open the link with no login, the same way Slack will. Waits up to ~1 minute."""
    for _ in range(6):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                return True
        except requests.RequestException:
            pass
        time.sleep(10)
    return False


def post_to_slack(payload):
    r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to send to Slack: {r.status_code} - {r.text}")


def send_report(d, image_url):
    post_to_slack({
        "text": "World Daily Metrics",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*World Daily Metrics*\n_{d['long_day']}_"}},
            {"type": "image", "image_url": image_url, "alt_text": f"World daily metrics for {d['day']}"},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "via Dune Analytics  ·  click the image to zoom"}]},
        ],
    })
    print("Report sent to Slack successfully!")


# ============================== main ==============================
def main():
    print(f"Starting World report at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    tv = fetch_dune_row(TRADES_VOL_QUERY_ID)
    orc = fetch_dune_row(ORACLE_QUERY_ID)
    fee = {r.get("stream"): r for r in fetch_dune_rows(FEES_QUERY_ID)}
    if not (tv or orc or fee):
        print("No data fetched from Dune. Exiting.")
        return

    d = build_data(tv, orc, fee)
    print()
    for n, (now, before, ch) in d["metrics"].items():
        print(f"{n}: {fmt(n, now)} vs {fmt(n, before)} ({sign(ch)})")

    path = render(d)
    print(f"\nSaved {path}")
    if not (CHART_REPO and CHART_TOKEN):
        print("GITHUB_TOKEN / GITHUB_REPOSITORY missing (not on GitHub Actions?), so the image is only saved.")
        return

    try:
        url = publish_to_github(path, d)
        if not link_works(url):
            raise RuntimeError(f"Slack could not open {url}. This happens when {CHART_REPO} is private: make it "
                               "public, or set CHART_REPO to a public repo (plus CHART_REPO_TOKEN).")
    except Exception as e:
        post_to_slack({"text": f"World Daily Metrics for {d['day']}: the image could not be published "
                               f"({str(e)[:200]}). Check the GitHub Actions log."})
        raise
    send_report(d, url)
    print("\nJob completed!")


if __name__ == "__main__":
    main()
