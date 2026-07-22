"""PDF report generator for the kalshi_soft superforecaster.

Assembles a clean, multi-page PDF from watchlist data, forecast records,
resolutions, calibration, run-log entries, and lessons learned.  Uses
matplotlib for charts (non-interactive Agg backend) and reportlab.platypus
for page layout.

Public API
----------
build_pdf(watchlist, forecasts, resolutions, calibration, run_log,
          lessons=None, out_path=config.LATEST_PDF_PATH) -> Path
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle,
    HRFlowable, KeepTogether,
)
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors

from lib import schemas, config, scoring, recledger

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_PAGE_W, _PAGE_H = A4
_MARGIN = 2 * cm


def _styles() -> dict:
    """Return a dict of ParagraphStyle objects for the report."""
    base = getSampleStyleSheet()
    s: dict = {}

    s["title"] = ParagraphStyle(
        "ReportTitle",
        parent=base["Title"],
        fontSize=22,
        leading=26,
        spaceAfter=6,
        textColor=colors.HexColor("#1A237E"),
    )
    s["subtitle"] = ParagraphStyle(
        "ReportSubtitle",
        parent=base["Normal"],
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#5C6BC0"),
        spaceAfter=4,
    )
    s["section"] = ParagraphStyle(
        "SectionHeader",
        parent=base["Heading1"],
        fontSize=13,
        leading=16,
        spaceBefore=14,
        spaceAfter=4,
        textColor=colors.HexColor("#283593"),
    )
    s["market_title"] = ParagraphStyle(
        "MarketTitle",
        parent=base["Heading2"],
        fontSize=11,
        leading=13,
        spaceBefore=10,
        spaceAfter=2,
        textColor=colors.HexColor("#1565C0"),
    )
    s["body"] = ParagraphStyle(
        "Body",
        parent=base["Normal"],
        fontSize=9,
        leading=12,
        spaceAfter=2,
    )
    s["small"] = ParagraphStyle(
        "Small",
        parent=base["Normal"],
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#555555"),
        spaceAfter=2,
    )
    s["footnote"] = ParagraphStyle(
        "Footnote",
        parent=base["Normal"],
        fontSize=7,
        leading=9,
        textColor=colors.HexColor("#777777"),
        spaceAfter=0,
    )
    return s


def _sp(n: float = 4) -> Spacer:
    return Spacer(1, n)


def _hr() -> HRFlowable:
    return HRFlowable(
        width="100%", thickness=0.5,
        color=colors.HexColor("#C5CAE9"),
        spaceAfter=4, spaceBefore=4,
    )


def _fmt_pct(v: Optional[float], decimals: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.{decimals}f}%"


def _fmt_float(v: Optional[float], decimals: int = 4) -> str:
    if v is None:
        return "—"
    return f"{v:.{decimals}f}"


def _edge_color(edge: Optional[float], lean: str) -> str:
    """Return hex color: green if edge favors the lean, red if against, grey if neutral."""
    if edge is None:
        return "#888888"
    if lean == "YES" and edge > 0:
        return "#2E7D32"
    if lean == "NO" and edge < 0:
        return "#2E7D32"
    if lean == "NONE":
        return "#888888"
    return "#C62828"


def _fmt_dollar(v: float) -> str:
    """Format a dollar value with sign and 2 decimal places, e.g. +$0.13 or -$0.05."""
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):.2f}"


def _profitability_lines(
    cur: schemas.ForecastEntry,
    st: dict,
    close_time: str = "",
) -> list:
    """Return a list of Paragraphs for the explicit trade lines, or [] if no ev data.

    Parameters
    ----------
    cur:
        The current ForecastEntry.
    st:
        Paragraph styles dict.
    close_time:
        ISO-8601 close timestamp for the market (used in the limit-order line).
        Comes from the WatchlistEntry or ForecastRecord, not ForecastEntry itself.
    """
    if cur.ev_per_contract is None:
        return []

    ev = cur.ev_per_contract
    lean = cur.lean or "NONE"
    fee = cur.fee_per_contract  # may be None for older records

    if lean == "NONE":
        ev_str = _fmt_dollar(ev) if ev is not None else "n/a"
        note = getattr(cur, "lean_note", None)
        if note:
            # Explain why there's no actionable lean (modal side overpriced, or confidence-gated).
            tail = (f" (Indicative raw EV {ev_str}/contract at my probability, not recommended.)"
                    if ev is not None and ev > 0 else "")
            text = f'<font color="#888888">No actionable lean: {note}{tail}</font>'
        else:
            text = (
                f'<font color="#888888">No profitable trade after fees '
                f"(best net EV {ev_str}/contract at spot).</font>"
            )
        return [Paragraph(text, st["body"])]

    # Leaned market — determine the ask price for the lean side
    ask = cur.yes_ask if lean == "YES" else cur.no_ask

    ev_color = "#2E7D32" if ev > 0 else "#C62828"
    ev_str = _fmt_dollar(ev)

    # Build spot trade line
    ask_str = f"${ask:.2f}" if ask is not None else "—"
    fee_str = f"${fee:.2f}" if fee is not None else "—"

    close_date = close_time[:10] if close_time else ""

    spot_text = (
        f"Trade (spot): BUY {lean} @ {ask_str} now "
        f"(taker, fee {fee_str}/contract) -> "
        f'net EV <font color="{ev_color}"><b>{ev_str}/contract</b></font>'
    )
    result = [Paragraph(spot_text, st["body"])]

    # Build limit trade line (only if both limit_price and ev_limit_per_contract are present)
    if cur.limit_price is not None and cur.ev_limit_per_contract is not None:
        lev = cur.ev_limit_per_contract
        lev_color = "#2E7D32" if lev > 0 else "#C62828"
        lev_str = _fmt_dollar(lev)
        limit_str = f"${cur.limit_price:.2f}"
        close_part = f" until {close_date}" if close_date else ""
        limit_text = (
            f"Trade (limit): rest BUY {lean} @ {limit_str} GTC{close_part} -> "
            f'net EV <font color="{lev_color}"><b>{lev_str}/contract</b></font> if filled'
        )
        result.append(Paragraph(limit_text, st["body"]))

    return result


def _parse_dt(ts: str) -> Optional[_dt.datetime]:
    if not ts:
        return None
    try:
        return schemas.parse_iso(ts)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Chart builders — each saves a PNG to SCRATCH_DIR and returns the path
# ---------------------------------------------------------------------------

def _drift_chart(record: schemas.ForecastRecord, out_path: Path) -> Optional[Path]:
    """Build a small drift chart (my_prob vs market_implied over time)."""
    series = scoring.drift_series(record)
    if not series:
        return None

    dates = []
    my_probs: list[float] = []
    mkt_probs: list[float] = []
    mkt_dates = []

    for ts, my_p, mkt_p in series:
        dt = _parse_dt(ts)
        if dt is None:
            continue
        dates.append(dt)
        my_probs.append(my_p)
        if mkt_p is not None:
            mkt_dates.append(dt)
            mkt_probs.append(mkt_p)

    if not dates:
        return None

    fig, ax = plt.subplots(figsize=(4.5, 1.8))
    ax.plot(dates, my_probs, "o-", color="#1565C0", linewidth=1.5,
            markersize=3, label="Mine")
    if mkt_dates:
        ax.plot(mkt_dates, mkt_probs, "s--", color="#EF6C00", linewidth=1,
                markersize=3, label="Market")
    ax.set_ylim(0, 1)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=2, maxticks=5))
    ax.tick_params(axis="both", labelsize=6)
    ax.legend(fontsize=6, loc="upper left", framealpha=0.5)
    ax.set_title(f"Drift — {record.ticker}", fontsize=7, pad=2)
    ax.grid(True, linestyle=":", linewidth=0.4, alpha=0.5)
    fig.tight_layout(pad=0.4)
    fig.savefig(str(out_path), dpi=110, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _edge_bar_chart(
    tickers: list[str], edges: list[Optional[float]], out_path: Path
) -> Optional[Path]:
    """Horizontal bar chart of edge values, sorted."""
    pairs = [(t, e) for t, e in zip(tickers, edges) if e is not None]
    if not pairs:
        return None
    pairs.sort(key=lambda x: x[1])
    labels = [p[0] for p in pairs]
    vals = [p[1] for p in pairs]
    bar_colors = ["#2E7D32" if v >= 0 else "#C62828" for v in vals]

    fig, ax = plt.subplots(figsize=(5.5, max(1.5, 0.35 * len(labels) + 0.5)))
    bars = ax.barh(labels, vals, color=bar_colors, height=0.6)
    ax.axvline(0, color="#333333", linewidth=0.8)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v*100:+.0f}pp"))
    ax.tick_params(axis="both", labelsize=7)
    ax.set_title("Edge vs Market (pp = percentage points)", fontsize=8, pad=3)
    ax.grid(True, axis="x", linestyle=":", linewidth=0.4, alpha=0.5)
    for bar, val in zip(bars, vals):
        ax.text(
            val + (0.002 if val >= 0 else -0.002),
            bar.get_y() + bar.get_height() / 2,
            f"{val*100:+.1f}",
            va="center", ha="left" if val >= 0 else "right",
            fontsize=6, color="#333333",
        )
    fig.tight_layout(pad=0.5)
    fig.savefig(str(out_path), dpi=110, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _reliability_diagram(calibration: schemas.Calibration, out_path: Path) -> Optional[Path]:
    """Reliability diagram (mean_forecast vs observed_freq per bin)."""
    bins = [b for b in calibration.bins if b.n > 0 and
            b.mean_forecast is not None and b.observed_freq is not None]
    if not bins:
        return None

    mf = [b.mean_forecast for b in bins]
    of_ = [b.observed_freq for b in bins]
    ns = [b.n for b in bins]

    fig, ax = plt.subplots(figsize=(3.5, 3.0))
    ax.plot([0, 1], [0, 1], "--", color="#AAAAAA", linewidth=1, label="Perfect")
    ax.scatter(mf, of_, s=[max(20, n * 8) for n in ns],
               c="#1565C0", alpha=0.75, zorder=3)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Mean Forecast", fontsize=7)
    ax.set_ylabel("Observed Freq", fontsize=7)
    ax.set_title("Reliability Diagram", fontsize=8, pad=3)
    ax.tick_params(labelsize=6)
    ax.legend(fontsize=6, loc="upper left")
    ax.grid(True, linestyle=":", linewidth=0.4, alpha=0.5)
    fig.tight_layout(pad=0.5)
    fig.savefig(str(out_path), dpi=110, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _usage_trend_chart(run_log: list[schemas.RunLogEntry], out_path: Path) -> Optional[Path]:
    """Trend line chart of est_tokens and tool_calls per run."""
    entries = run_log[-30:]

    # Try date-based x-axis first
    points_dt = []
    for e in entries:
        dt = _parse_dt(e.run_id)
        if dt is not None:
            points_dt.append((dt, e.usage.est_tokens, e.usage.tool_calls))

    use_dates = len(points_dt) >= 2

    if use_dates:
        xs = [p[0] for p in points_dt]
        tokens_y = [p[1] for p in points_dt]
        tools_y = [p[2] for p in points_dt]
    else:
        # Fall back to index-based
        if not entries:
            return None
        xs = list(range(len(entries)))
        tokens_y = [e.usage.est_tokens for e in entries]
        tools_y = [e.usage.tool_calls for e in entries]

    has_tokens = any(t is not None and t > 0 for t in tokens_y)
    has_tools = any(t is not None and t > 0 for t in tools_y)
    if not has_tokens and not has_tools:
        return None

    fig, ax1 = plt.subplots(figsize=(5.0, 2.2))
    ax2 = ax1.twinx()

    if has_tokens:
        clean_t = [t if t is not None else 0 for t in tokens_y]
        ax1.plot(xs, clean_t, "o-", color="#1565C0", linewidth=1.5, markersize=3,
                 label="est_tokens")
        ax1.set_ylabel("Est. Tokens", fontsize=7, color="#1565C0")
        ax1.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda v, _: f"{int(v):,}")
        )
    else:
        ax1.set_yticks([])

    if has_tools:
        ax2.plot(xs, tools_y, "s--", color="#EF6C00", linewidth=1, markersize=3,
                 label="tool_calls")
        ax2.set_ylabel("Tool Calls", fontsize=7, color="#EF6C00")
    else:
        ax2.set_yticks([])

    if use_dates:
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax1.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=2, maxticks=6))
    else:
        ax1.set_xlabel("Run index", fontsize=7)

    ax1.tick_params(labelsize=6)
    ax2.tick_params(labelsize=6)
    ax1.set_title("Usage Trend (last ~30 runs)", fontsize=8, pad=3)
    ax1.grid(True, linestyle=":", linewidth=0.4, alpha=0.5)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    all_lines = lines1 + lines2
    all_labels = labels1 + labels2
    if all_lines:
        ax1.legend(all_lines, all_labels, fontsize=6, loc="upper left")

    fig.tight_layout(pad=0.5)
    fig.savefig(str(out_path), dpi=110, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _performance_over_time_chart(
    resolutions: schemas.ResolutionsFile,
    out_path: Path,
) -> Optional[Path]:
    """Cumulative Brier (mine vs market) and running skill, in resolution order.

    This is the "am I getting better as the sample grows?" view: at each resolved
    market we plot the mean Brier so far (mine and market) and their gap (skill).
    Lower Brier is better; skill > 0 means we are beating the market cumulatively.
    """
    resolved = list(resolutions.resolved)
    if len(resolved) < 2:
        return None

    def _key(r):
        dt = _parse_dt(r.resolved_at)
        return (dt is None, dt or r.resolved_at)

    resolved = sorted(resolved, key=_key)

    xs, cum_mine, cum_mkt, cum_skill = [], [], [], []
    mine_sum = mine_n = 0.0
    mkt_sum = mkt_n = 0.0
    for i, r in enumerate(resolved, start=1):
        bm = r.brier_mine
        if bm is None:
            bm = scoring.brier(r.final_my_probability, r.outcome)
        mine_sum += bm
        mine_n += 1
        if r.final_market_implied is not None:
            bk = r.brier_market
            if bk is None:
                bk = scoring.brier(r.final_market_implied, r.outcome)
            mkt_sum += bk
            mkt_n += 1
        dt = _parse_dt(r.resolved_at)
        xs.append(dt if dt is not None else i)
        cm = mine_sum / mine_n
        ck = (mkt_sum / mkt_n) if mkt_n else None
        cum_mine.append(cm)
        cum_mkt.append(ck)
        cum_skill.append((ck - cm) if ck is not None else None)

    use_dates = all(not isinstance(x, int) for x in xs)

    fig, ax1 = plt.subplots(figsize=(5.6, 2.6))
    ax1.plot(xs, cum_mine, "o-", color="#1565C0", linewidth=1.6, markersize=3,
             label="Brier — mine (cum.)")
    # Market line: skip None gaps.
    mkt_x = [x for x, y in zip(xs, cum_mkt) if y is not None]
    mkt_y = [y for y in cum_mkt if y is not None]
    if mkt_y:
        ax1.plot(mkt_x, mkt_y, "s--", color="#EF6C00", linewidth=1.3, markersize=3,
                 label="Brier — market (cum.)")
    ax1.set_ylabel("Cumulative Brier (lower = better)", fontsize=7)
    ax1.tick_params(labelsize=6)
    ax1.grid(True, linestyle=":", linewidth=0.4, alpha=0.5)

    ax2 = ax1.twinx()
    sk_x = [x for x, y in zip(xs, cum_skill) if y is not None]
    sk_y = [y for y in cum_skill if y is not None]
    if sk_y:
        ax2.plot(sk_x, sk_y, "-", color="#2E7D32", linewidth=1.0, alpha=0.7,
                 label="Skill vs market (cum.)")
        ax2.axhline(0.0, color="#999999", linewidth=0.6, linestyle=":")
    ax2.set_ylabel("Skill (market − mine)", fontsize=7, color="#2E7D32")
    ax2.tick_params(labelsize=6)

    if use_dates:
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax1.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=2, maxticks=6))
    else:
        ax1.set_xlabel("Resolution #", fontsize=7)

    ax1.set_title("Performance Over Time (by resolution)", fontsize=8, pad=3)
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    if lines1 or lines2:
        ax1.legend(lines1 + lines2, labels1 + labels2, fontsize=6, loc="upper right")

    fig.tight_layout(pad=0.5)
    fig.savefig(str(out_path), dpi=110, bbox_inches="tight")
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Section builders — each returns a list of flowables
# ---------------------------------------------------------------------------

def _build_cover(
    watchlist: schemas.Watchlist,
    forecasts: list[schemas.ForecastRecord],
    resolutions: schemas.ResolutionsFile,
    calibration: schemas.Calibration,
    st: dict,
) -> list:
    """Build the cover / summary section."""
    elems: list = []
    elems.append(Paragraph("Superforecaster — Kalshi Soft Markets", st["title"]))
    elems.append(Paragraph(f"Generated: {schemas.utc_now_iso()}", st["subtitle"]))
    elems.append(_hr())
    elems.append(_sp(6))

    active_count = len(watchlist.active())
    resolved_count = len(resolutions.resolved)
    total_forecasts = sum(len(f.history) for f in forecasts)

    # Count profitable leans (lean != NONE and ev_per_contract is not None)
    forecast_map = {f.ticker: f for f in forecasts}
    profitable_count = 0
    for m in watchlist.active():
        rec = forecast_map.get(m.ticker)
        if rec and rec.current:
            cur = rec.current
            if (cur.lean or "NONE") != "NONE" and cur.ev_per_contract is not None:
                profitable_count += 1

    summary_data = [
        ["Metric", "Value"],
        ["Active markets", str(active_count)],
        ["Resolved markets", str(resolved_count)],
        ["Total forecast entries", str(total_forecasts)],
        ["Profitable leans (after fees)", str(profitable_count)],
    ]

    if calibration.n_resolved > 0:
        summary_data += [
            ["Resolved with calibration", str(calibration.n_resolved)],
            ["Brier (mine, mean)", _fmt_float(calibration.brier_mine_mean)],
            ["Brier (market, mean)", _fmt_float(calibration.brier_market_mean)],
            ["Skill vs market", _fmt_float(calibration.skill_vs_market)],
        ]

    tbl = Table(summary_data, colWidths=[9 * cm, 5 * cm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#283593")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.HexColor("#EEF2FF"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C5CAE9")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    elems.append(tbl)

    if active_count == 0 and resolved_count == 0 and total_forecasts == 0:
        elems.append(_sp(12))
        elems.append(Paragraph(
            "No data yet — run the agent to populate forecasts.",
            st["small"],
        ))

    return elems


# Below this many resolved markets, a Brier/skill gap is not yet distinguishable
# from luck — we surface it but label it provisional rather than letting the
# headline number read as a proven edge.
_MIN_RESOLUTIONS_FOR_SIGNAL = 30


def _segment_table(title: str, segments: dict, st: dict, label_header: str) -> list:
    """Render one performance table from a by_category / by_segment dict.

    *segments* maps a label -> {n, brier_mine_mean, brier_market_mean,
    skill_vs_market}. Rows are sorted by skill (best first); rows with no market
    comparison (skill None) sink to the bottom.
    """
    elems: list = []
    if not segments:
        return elems

    def _sort_key(item):
        skill = item[1].get("skill_vs_market")
        return (skill is None, -(skill if skill is not None else 0.0))

    rows = sorted(segments.items(), key=_sort_key)

    data = [[label_header, "n", "Brier (mine)", "Brier (mkt)", "Skill"]]
    for label, s in rows:
        skill = s.get("skill_vs_market")
        skill_str = f"{skill:+.3f}" if skill is not None else "—"
        data.append([
            label,
            str(s.get("n", 0)),
            _fmt_float(s.get("brier_mine_mean"), 3),
            _fmt_float(s.get("brier_market_mean"), 3),
            skill_str,
        ])

    tbl = Table(data, colWidths=[7.5 * cm, 1.2 * cm, 2.4 * cm, 2.4 * cm, 2.0 * cm])
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#283593")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.HexColor("#EEF2FF"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C5CAE9")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
    ]
    # Tint the Skill column green/red by sign.
    for i, (_, s) in enumerate(rows, start=1):
        skill = s.get("skill_vs_market")
        if skill is None:
            continue
        col = colors.HexColor("#1B5E20") if skill > 0 else colors.HexColor("#B71C1C")
        style.append(("TEXTCOLOR", (4, i), (4, i), col))
    tbl.setStyle(TableStyle(style))

    elems.append(Paragraph(title, st["small"]))
    elems.append(_sp(2))
    elems.append(tbl)
    return elems


_TBL_STYLE = [
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#283593")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#EEF2FF"), colors.white]),
    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C5CAE9")),
    ("TOPPADDING", (0, 0), (-1, -1), 3),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ("ALIGN", (1, 0), (-1, -1), "CENTER"),
]


def _sb_row(label: str, sb: dict) -> list[str]:
    n = sb["n_scored"]
    return [
        label, f"{sb['n_resolved']}", f"{n}", f"{sb['n_nofill']}",
        f"{sb['wins']}/{n} ({sb['win_rate']:.0%})" if n else "—",
        _fmt_dollar(sb["pnl"]) if n else "—",
        f"${sb['deployed']:.2f}" if n else "—",
        f"{sb['roi']:+.1%}" if sb["roi"] is not None else "—",
        f"{sb['beat_market']}/{n}" if n else "—",
    ]


def _build_structural_edge(st: dict) -> list:
    """THE MONEY PAGE (Workstream A3, PLAN_FOR_OPUS.md).

    The structural/atlas edge is the project's one engine with measured out-of-sample edge; this
    section is its live, forward verification record — conservative (fills-evidenced) numbers
    first, legacy/optimistic clearly quarantined, tail stress, and the precommitted verification
    bar. The user reads ONE artifact; the P&L engine lives on its front page."""
    elems: list = []
    rows = recledger.load_rows()
    elems.append(Paragraph("Structural Edge — the money engine (paper)", st["section"]))
    if not rows:
        elems.append(Paragraph("No trade recommendations logged yet.", st["body"]))
        return elems

    vpol = recledger.load_verification_policy()
    stress = recledger.compute_stress(rows)
    vs = recledger.verification_status(rows, vpol, stress)

    elems.append(Paragraph(
        "Live forward record of the history-learned market-calibration edge "
        "(<i>lib/atlas</i>: mid-liquidity longshot fades, +EV after fee &amp; half-spread). "
        "The <b>conservative</b> column is the official number: a rec counts only if the "
        "rec-time orderbook snapshot proves the limit was marketable, and it fills at the real "
        "ask. The legacy cohort predates fill evidence and is provisional only. "
        "<b>All positions are PAPER — no live trading until the verification bar passes and "
        "the user signs off (constraint 2026-07-17).</b>", st["body"]))
    elems.append(_sp(4))

    # --- scoreboard table (official first) ---
    data = [["Cohort", "Resolved", "Scored", "No-fill", "Win rate", "P&L", "Deployed",
             "ROI", "Beat mkt"]]
    data.append(_sb_row("VERIFIED (conservative — official)",
                        recledger.scoreboard(rows, "verified", "conservative")))
    legacy = recledger.scoreboard(rows, "legacy", "optimistic")
    if legacy["n_resolved"]:
        data.append(_sb_row("legacy (optimistic — fills unverified)", legacy))
    tbl = Table(data, colWidths=[5.6 * cm, 1.6 * cm, 1.4 * cm, 1.4 * cm, 2.2 * cm,
                                 1.5 * cm, 1.7 * cm, 1.4 * cm, 1.6 * cm])
    tbl.setStyle(TableStyle(_TBL_STYLE))
    elems.append(tbl)
    elems.append(_sp(6))

    # --- verification bar progress ---
    elems.append(Paragraph(
        "Verification bar (precommitted, PLAN_FOR_OPUS §A4) — passing unlocks the live-trading "
        f"<i>conversation</i> only: <b>{'PASSED' if vs['verified'] else 'NOT PASSED'}</b>",
        st["small"]))
    vdata = [["Criterion", "Target", "Current", "Pass"]]
    for name, c in vs["criteria"].items():
        cur = c["current"]
        if isinstance(cur, float):
            cur = f"{cur:+.3f}"
        elif isinstance(cur, tuple):
            cur = f"[{cur[0]:+.3f}, {cur[1]:+.3f}]"
        elif isinstance(cur, dict):
            cur = ", ".join(f"{k}={v if not isinstance(v, float) else round(v, 3)}"
                            for k, v in cur.items())
        # Paragraph cells so long target/current strings wrap instead of overflowing
        vdata.append([name, Paragraph(str(c["target"]), st["footnote"]),
                      Paragraph(str(cur), st["footnote"]), "PASS" if c["pass"] else "—"])
    vtbl = Table(vdata, colWidths=[4.2 * cm, 5.6 * cm, 6.2 * cm, 1.4 * cm])
    vstyle = list(_TBL_STYLE)
    for i, (_, c) in enumerate(vs["criteria"].items(), start=1):
        col = colors.HexColor("#1B5E20") if c["pass"] else colors.HexColor("#9E9E9E")
        vstyle.append(("TEXTCOLOR", (3, i), (3, i), col))
    vtbl.setStyle(TableStyle(vstyle))
    elems.append(vtbl)
    elems.append(_sp(6))

    # --- tail stress ---
    if stress:
        elems.append(Paragraph(
            f"<b>Tail stress</b> (Monte Carlo, {stress['n_future']} future recs × "
            f"{stress['trials']} trials, calibrated probabilities as truth): "
            f"P(ROI&lt;0) = <b>{stress['p_loss']:.1%}</b>; ROI mean {stress['roi_mean']:+.1%} "
            f"(p5 {stress['roi_p5']:+.1%} / p95 {stress['roi_p95']:+.1%}); expected max "
            f"drawdown {stress['expected_max_drawdown']:.2f} units; break-even win rate "
            f"{stress['breakeven_win_rate']:.1%}. A 13-0 start does NOT survive this section "
            "unexamined: one tail hit erases 3–8 wins.", st["body"]))
        elems.append(_sp(4))

    # --- per-cell (verified) ---
    cells = recledger.per_cell(rows, "verified", "conservative")
    if cells:
        cdata = [["Cell", "Scored", "Win rate", "P&L", "ROI"]]
        for cell, sb in sorted(cells.items(), key=lambda kv: -(kv[1]["roi"] or 0)):
            n = sb["n_scored"]
            cdata.append([cell, str(n),
                          f"{sb['win_rate']:.0%}" if n else "—",
                          _fmt_dollar(sb["pnl"]) if n else "—",
                          f"{sb['roi']:+.1%}" if sb["roi"] is not None else "—"])
        ctbl = Table(cdata, colWidths=[7.0 * cm, 1.6 * cm, 2.0 * cm, 2.0 * cm, 2.0 * cm])
        ctbl.setStyle(TableStyle(_TBL_STYLE))
        elems.append(Paragraph("Per-cell record (verified cohort — the kill-switch view: "
                               f"a cell dies at n≥{vpol['cell_kill_min_n']} with ROI&lt;0)",
                               st["small"]))
        elems.append(_sp(2))
        elems.append(ctbl)
        elems.append(_sp(6))

    # --- paper broker (Workstream D2): execution simulation on top of the signal ledger ---
    try:
        from lib import broker as _broker
        orders = _broker.load_orders()
        if orders:
            s_by: dict = {}
            for o in orders:
                s_by[o.get("status") or "?"] = s_by.get(o.get("status") or "?", 0) + 1
            eq = _broker.equity_stats(orders)
            settled_o = [o for o in orders if o.get("status") == "settled"]
            pnl = sum(o.get("realized_pnl") or 0.0 for o in settled_o)
            cost = sum((o.get("fill_price") or 0) * (o.get("filled_qty") or 0)
                       + (o.get("fee_paid") or 0) for o in settled_o)
            terminal = [o for o in orders
                        if o.get("status") in ("settled", "expired", "partial_expired")]
            nofill = sum(1 for o in orders if o.get("status") == "expired")
            elems.append(Paragraph(
                f"<b>Paper broker</b> (resting-limit simulation, D1 sizing on a notional "
                f"${eq['bankroll_notional']:.0f}): {len(orders)} orders "
                f"({', '.join(f'{k} {v}' for k, v in sorted(s_by.items()))}). "
                f"Settled P&amp;L ${pnl:+.2f} on ${cost:.2f}"
                + (f" (ROI {pnl / cost:+.1%})" if cost else "") + "; no-fill rate "
                + (f"{nofill / len(terminal):.0%}" if terminal else "— (none terminal yet)")
                + f"; equity ${eq['equity']:.2f}, deployed ${eq['open_deployed']:.2f}, "
                f"drawdown {eq['current_drawdown']:.1%}"
                + (" — <b>DRAWDOWN HALT</b>" if eq["halted"] else "") + ".", st["body"]))
            elems.append(_sp(4))
    except Exception:
        pass

    # --- open recs ---
    open_recs = [r for r in rows if r.get("status") != "resolved"]
    if open_recs:
        odata = [["Ticker", "Side", "Mkt YES", "Fair", "Entry", "EVnet", "Fillable@rec"]]
        for r in open_recs[-12:]:
            ev = r.get("fill_evidence") or {}
            odata.append([r.get("ticker", "?"), r.get("side", "?"),
                          f"{r.get('market_yes', 0):.2f}", f"{r.get('calibrated_yes', 0):.2f}",
                          f"{r.get('entry_limit', 0):.2f}", f"{r.get('ev_net', 0):+.3f}",
                          ("yes" if ev.get("fillable_now") else
                           ("resting" if ev else "no evidence"))])
        otbl = Table(odata, colWidths=[6.4 * cm, 1.2 * cm, 1.6 * cm, 1.4 * cm, 1.4 * cm,
                                       1.6 * cm, 2.6 * cm])
        otbl.setStyle(TableStyle(_TBL_STYLE))
        elems.append(Paragraph("Open recommendations (paper basket — correlated longshot fade; "
                               "size small &amp; equal; one thematic position)", st["small"]))
        elems.append(_sp(2))
        elems.append(otbl)

    return elems


def _build_performance(
    resolutions: schemas.ResolutionsFile,
    calibration: schemas.Calibration,
    scratch_dir: Path,
    st: dict,
) -> list:
    """Performance-over-time + per-segment skill breakdown (gated on n_resolved >= 1)."""
    elems: list = []
    n = calibration.n_resolved
    if n < 1:
        return elems

    elems.append(Paragraph("Performance Over Time", st["section"]))
    elems.append(_hr())

    # Headline KPIs with an explicit significance caveat.
    bm = calibration.brier_mine_mean
    bk = calibration.brier_market_mean
    sk = calibration.skill_vs_market
    verdict = "beating" if (sk is not None and sk > 0) else "trailing"
    kpi = (
        f"Resolved: <b>{n}</b> &nbsp; Brier (mine): <b>{_fmt_float(bm, 3)}</b> "
        f"vs market <b>{_fmt_float(bk, 3)}</b> &nbsp; "
        f"Skill: <b>{(f'{sk:+.3f}' if sk is not None else '—')}</b> "
        f"({verdict} the market)"
    )
    elems.append(Paragraph(kpi, st["body"]))

    if n < _MIN_RESOLUTIONS_FOR_SIGNAL:
        elems.append(Paragraph(
            f'<font color="#B71C1C">Provisional — only {n} resolved market(s). '
            f"A skill gap is not statistically distinguishable from luck below "
            f"~{_MIN_RESOLUTIONS_FOR_SIGNAL} resolutions; read the trend, not the point estimate.</font>",
            st["small"],
        ))
    elems.append(_sp(6))

    # Cumulative trend chart.
    chart_path = scratch_dir / "_performance_trend.png"
    saved = _performance_over_time_chart(resolutions, chart_path)
    if saved and saved.exists():
        elems.append(Image(str(saved), width=13 * cm, height=6.0 * cm))
    else:
        elems.append(Paragraph(
            "Need at least 2 resolved markets to plot a trend.", st["small"]
        ))
    elems.append(_sp(8))

    # Per-segment skill — where the edge is (and isn't).
    elems.append(Paragraph("Where the edge is — by segment", st["small"]))

    # Best/worst callout among subcategories with at least 2 resolutions.
    seg = calibration.by_segment or {}
    rated = {k: v for k, v in seg.items()
             if v.get("n", 0) >= 2 and v.get("skill_vs_market") is not None}
    if rated:
        best = max(rated.items(), key=lambda kv: kv[1]["skill_vs_market"])
        worst = min(rated.items(), key=lambda kv: kv[1]["skill_vs_market"])
        callout = (
            f'Strongest: <b>{best[0]}</b> (skill {best[1]["skill_vs_market"]:+.3f}, '
            f'n={best[1]["n"]}). &nbsp; Weakest: <b>{worst[0]}</b> '
            f'(skill {worst[1]["skill_vs_market"]:+.3f}, n={worst[1]["n"]}).'
        )
        elems.append(Paragraph(callout, st["small"]))
    else:
        elems.append(Paragraph(
            '<font color="#888888">No segment has ≥2 resolved markets yet — '
            "per-segment skill is still single-sample noise.</font>",
            st["small"],
        ))
    elems.append(_sp(4))

    elems.extend(_segment_table(
        "By category", calibration.by_category or {}, st, "Category"))
    elems.append(_sp(6))
    elems.extend(_segment_table(
        "By sub-category", seg, st, "Category / sub-category"))

    return elems


def _profit_table(title: str, segments: dict, st: dict, label_header: str) -> list:
    """Render one realized-P&L table from a profit_by_* dict.

    *segments* maps a label -> {n_trades, total_pnl, total_staked, roi, win_rate,
    avg_clv, max_drawdown}. Rows are sorted by ROI (best first); ties broken by P&L.
    """
    elems: list = []
    if not segments:
        return elems

    def _sort_key(item):
        roi = item[1].get("roi")
        return (roi is None, -(roi if roi is not None else 0.0))

    rows = sorted(segments.items(), key=_sort_key)

    data = [[label_header, "Trades", "P&L", "ROI", "Win%", "MaxDD"]]
    for label, s in rows:
        roi = s.get("roi")
        wr = s.get("win_rate")
        data.append([
            label,
            str(s.get("n_trades", 0)),
            _fmt_dollar(s.get("total_pnl", 0.0)),
            (f"{roi:+.1%}" if roi is not None else "—"),
            (f"{wr:.0%}" if wr is not None else "—"),
            _fmt_dollar(s.get("max_drawdown", 0.0)),
        ])

    tbl = Table(data, colWidths=[7.5 * cm, 1.6 * cm, 2.2 * cm, 1.9 * cm, 1.5 * cm, 2.0 * cm])
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#283593")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.HexColor("#EEF2FF"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C5CAE9")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
    ]
    # Tint the P&L + ROI columns green/red by sign.
    for i, (_, s) in enumerate(rows, start=1):
        pnl = s.get("total_pnl")
        if pnl is not None:
            col = colors.HexColor("#1B5E20") if pnl > 0 else colors.HexColor("#B71C1C")
            style.append(("TEXTCOLOR", (2, i), (3, i), col))
    tbl.setStyle(TableStyle(style))

    elems.append(Paragraph(title, st["small"]))
    elems.append(_sp(2))
    elems.append(tbl)
    return elems


def _build_profit_and_loss(
    calibration: schemas.Calibration,
    st: dict,
) -> list:
    """Realized Profit & Loss — the dollar dimension that Brier alone can't see.

    A forecast can be well-calibrated yet lose money; this section makes that
    visible by scoring every resolved YES/NO lean on realized P&L, ROI, and win
    rate. NONE-leans (no position taken) are excluded from the trade counts.
    """
    elems: list = []
    pbc = calibration.profit_by_category or {}
    pbs = calibration.profit_by_strategy or {}
    if not pbc and not pbs:
        return elems

    # Portfolio-wide rollup across all trades (category partition covers every trade).
    n_trades = sum(s.get("n_trades", 0) for s in pbc.values())
    if n_trades == 0:
        return elems
    total_pnl = sum(s.get("total_pnl", 0.0) for s in pbc.values())
    total_staked = sum(s.get("total_staked", 0.0) for s in pbc.values())
    total_wins = sum((s.get("win_rate") or 0.0) * s.get("n_trades", 0) for s in pbc.values())
    roi = (total_pnl / total_staked) if total_staked > 0 else None
    win_rate = (total_wins / n_trades) if n_trades else None

    elems.append(Paragraph("Profit &amp; Loss (realized)", st["section"]))
    elems.append(_hr())

    pnl_color = "#1B5E20" if total_pnl > 0 else "#B71C1C"
    kpi = (
        f"Resolved trades: <b>{n_trades}</b> &nbsp; "
        f'Realized P&amp;L: <b><font color="{pnl_color}">{_fmt_dollar(total_pnl)}</font></b> '
        f"per contract &nbsp; "
        f"ROI: <b>{(f'{roi:+.1%}' if roi is not None else '—')}</b> &nbsp; "
        f"Win rate: <b>{(f'{win_rate:.0%}' if win_rate is not None else '—')}</b>"
    )
    elems.append(Paragraph(kpi, st["body"]))
    elems.append(Paragraph(
        '<font color="#888888">Paper trade: buy the lean side at its entry ask, '
        "after Kalshi fees. A high win rate with negative ROI means winners are "
        "underpriced relative to the losers — calibration without profit.</font>",
        st["small"],
    ))
    elems.append(_sp(6))

    elems.extend(_profit_table("By category", pbc, st, "Category"))
    return elems


def _build_strategy_scoreboard(
    calibration: schemas.Calibration,
    st: dict,
) -> list:
    """Strategy Scoreboard — the experiment's verdict: which forecasting topology wins.

    Joins each arm's Brier skill (by_strategy) with its realized ROI
    (profit_by_strategy) so a single table answers 'what's actually working'.
    """
    elems: list = []
    bs = calibration.by_strategy or {}
    ps = calibration.profit_by_strategy or {}
    if not bs:
        return elems

    elems.append(Paragraph("Strategy Scoreboard", st["section"]))
    elems.append(_hr())
    elems.append(Paragraph(
        "Every forecast is tagged with the strategy arm that produced it; resolutions "
        "score that arm on both Brier skill and realized profit. The topology is "
        "measured, not assumed.", st["body"]))
    elems.append(_sp(4))

    # Sort arms by skill (best first), then ROI.
    def _sort_key(item):
        sk = item[1].get("skill_vs_market")
        roi = (ps.get(item[0]) or {}).get("roi")
        return (sk is None, -(sk if sk is not None else 0.0), -(roi if roi is not None else 0.0))

    rows = sorted(bs.items(), key=_sort_key)
    data = [["Strategy", "n", "Brier", "Skill", "Trades", "ROI"]]
    for sid, s in rows:
        p = ps.get(sid) or {}
        sk = s.get("skill_vs_market")
        roi = p.get("roi")
        data.append([
            sid,
            str(s.get("n", 0)),
            _fmt_float(s.get("brier_mine_mean"), 3),
            (f"{sk:+.3f}" if sk is not None else "—"),
            str(p.get("n_trades", 0)),
            (f"{roi:+.1%}" if roi is not None else "—"),
        ])

    tbl = Table(data, colWidths=[6.0 * cm, 1.2 * cm, 2.0 * cm, 2.0 * cm, 1.6 * cm, 1.9 * cm])
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#283593")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.HexColor("#EEF2FF"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C5CAE9")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
    ]
    for i, (sid, s) in enumerate(rows, start=1):
        sk = s.get("skill_vs_market")
        if sk is not None:
            col = colors.HexColor("#1B5E20") if sk > 0 else colors.HexColor("#B71C1C")
            style.append(("TEXTCOLOR", (3, i), (3, i), col))
        roi = (ps.get(sid) or {}).get("roi")
        if roi is not None:
            col = colors.HexColor("#1B5E20") if roi > 0 else colors.HexColor("#B71C1C")
            style.append(("TEXTCOLOR", (5, i), (5, i), col))
    tbl.setStyle(TableStyle(style))
    elems.append(tbl)

    if len(rows) == 1 and rows[0][0] == "untagged":
        elems.append(_sp(3))
        elems.append(Paragraph(
            '<font color="#888888">All resolved markets predate strategy tagging '
            "(arm = untagged). The scoreboard differentiates once strategy-tagged "
            "forecasts resolve.</font>",
            st["small"],
        ))
    return elems


def _cf_table(title: str, groups: dict, st: dict, label_header: str,
              order: Optional[list] = None) -> list:
    """Render one counterfactual-profitability table from a cf_by_* dict.

    *groups* maps a label -> {n, cf_pnl, cf_staked, cf_roi, cf_win_rate, max_drawdown}.
    Rows ordered by *order* if given (e.g. gap bands), else by ROI descending."""
    elems: list = []
    if not groups:
        return elems
    if order:
        rows = [(k, groups[k]) for k in order if k in groups]
        rows += [(k, v) for k, v in groups.items() if k not in order]
    else:
        rows = sorted(groups.items(),
                      key=lambda kv: (kv[1].get("cf_roi") is None,
                                      -(kv[1].get("cf_roi") or 0.0)))
    data = [[label_header, "n", "CF P&L", "CF ROI", "Win%", "MaxDD"]]
    for label, s in rows:
        roi = s.get("cf_roi")
        wr = s.get("cf_win_rate")
        data.append([
            label,
            str(s.get("n", 0)),
            _fmt_dollar(s.get("cf_pnl", 0.0)),
            (f"{roi:+.1%}" if roi is not None else "—"),
            (f"{wr:.0%}" if wr is not None else "—"),
            _fmt_dollar(s.get("max_drawdown", 0.0)),
        ])
    tbl = Table(data, colWidths=[6.6 * cm, 1.2 * cm, 2.2 * cm, 2.0 * cm, 1.5 * cm, 2.0 * cm])
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1B5E20")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#E8F5E9"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#A5D6A7")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
    ]
    for i, (_, s) in enumerate(rows, start=1):
        roi = s.get("cf_roi")
        if roi is not None:
            col = colors.HexColor("#1B5E20") if roi > 0 else colors.HexColor("#B71C1C")
            style.append(("TEXTCOLOR", (2, i), (3, i), col))
    tbl.setStyle(TableStyle(style))
    elems.append(Paragraph(title, st["small"]))
    elems.append(_sp(2))
    elems.append(tbl)
    return elems


def _open_expected_pnl(forecasts: list, st: dict) -> list:
    """Forward-looking: expected net P&L (mark-to-model) on current OPEN modal-side
    positions, using my probability as truth. Shows profitability before resolution."""
    from lib import profit
    elems: list = []
    rows = []
    total_ev = 0.0
    for rec in forecasts:
        c = rec.current
        if not c or c.my_probability is None:
            continue
        side = profit.modal_side(c.my_probability)
        ask = c.yes_ask if side == "YES" else c.no_ask
        if ask is None:
            continue
        ev = profit.expected_pnl(c.my_probability, side, ask)
        total_ev += ev
        rows.append((rec.ticker, side, ev, c.lean))
    if not rows:
        return elems
    taken_ev = sum(ev for _, _, ev, lean in rows if lean in ("YES", "NO"))
    pos = sum(1 for _, _, ev, _ in rows if ev > 0)
    elems.append(Paragraph(
        f"Forward-looking expected P&amp;L on <b>{len(rows)}</b> open modal-side positions "
        f"(mark-to-model): <b>{_fmt_dollar(total_ev)}</b>/contract total "
        f"({pos} are +EV). Of that, <b>{_fmt_dollar(taken_ev)}</b> is on actionable leans "
        f"(the rest is counterfactual — positions the confidence gate is holding us out of).",
        st["small"]))
    return elems


def _build_profitability(
    calibration: schemas.Calibration,
    forecasts: list,
    st: dict,
) -> list:
    """Profitability — the project's north star, tracked like any other metric.

    Counterfactual P&L scores EVERY resolved forecast (modal-side hypothetical), not
    just taken leans, then conditions it on the dims that predict profit so we learn
    *when* trading pays. Plus forward-looking expected P&L on open positions."""
    elems: list = []
    p = calibration.profitability or {}
    overall = p.get("cf_overall") or {}
    if not overall:
        return elems

    elems.append(Paragraph("Profitability — when does the edge pay? (counterfactual)", st["section"]))
    elems.append(_hr())
    roi = overall.get("cf_roi")
    wr = overall.get("cf_win_rate")
    roi_color = "#1B5E20" if (roi or 0) > 0 else "#B71C1C"
    elems.append(Paragraph(
        f"Every resolved forecast scored as a modal-side paper trade ({overall.get('n', 0)} total, "
        f"vs only the taken leans in the realized section above). Overall counterfactual "
        f'ROI: <b><font color="{roi_color}">{(f"{roi:+.1%}" if roi is not None else "—")}</font></b>, '
        f"win rate {(f'{wr:.0%}' if wr is not None else '—')}. The breakdowns below isolate "
        f"<b>when</b> the strategy is profitable — the goal is profit, so this is the scoreboard "
        f"that matters most.", st["body"]))
    elems.append(_sp(6))

    # The two highest-signal lenses first: does the gate help, and at what edge do we profit?
    elems.extend(_cf_table("Confidence gate: taken leans vs the ones we declined",
                           p.get("cf_by_taken", {}), st, "Decision"))
    elems.append(_sp(6))
    elems.extend(_cf_table("By edge vs market (|my prob − market|) — where profit survives fees",
                           p.get("cf_by_gap", {}), st, "Edge band",
                           order=["0-5pt", "5-10pt", "10-20pt", "20pt+", "no-market"]))
    elems.append(_sp(6))
    elems.extend(_cf_table("By stated confidence — does confidence predict profit?",
                           p.get("cf_by_confidence", {}), st, "Confidence",
                           order=["high", "medium", "low", "unknown"]))
    elems.append(_sp(6))
    elems.extend(_cf_table("By category", p.get("cf_by_category", {}), st, "Category"))
    elems.append(_sp(6))
    elems.extend(_cf_table("By strategy arm", p.get("cf_by_strategy", {}), st, "Strategy"))
    elems.append(_sp(8))

    # Forward-looking open-position expected P&L.
    elems.append(Paragraph("Open positions — expected P&amp;L (forward-looking)", st["small"]))
    elems.append(_sp(2))
    elems.extend(_open_expected_pnl(forecasts, st))

    if calibration.n_resolved < _MIN_RESOLUTIONS_FOR_SIGNAL:
        elems.append(_sp(4))
        elems.append(Paragraph(
            f'<font color="#B71C1C">Provisional — {calibration.n_resolved} resolved. '
            f"Profit ROI is even noisier than Brier at small n; read the direction, not the digits.</font>",
            st["small"]))
    return elems


def _build_learning(st: dict) -> list:
    """Autonomous Learning — the current learnable policy and the pending proposals the
    system has generated from its OWN record (scripts/learn_policy.py). Makes the
    self-tuning loop visible: what it wants to change, and whether the guardrails allow it."""
    from lib import policy as _policy, config as _config, store as _store
    elems: list = []
    pol = _policy.load()
    proposals_doc = _store.read_json(_config.DATA_DIR / "policy_proposals.json") or {}
    proposals = proposals_doc.get("proposals", [])

    elems.append(Paragraph("Autonomous Learning — policy &amp; proposals", st["section"]))
    elems.append(_hr())
    elems.append(Paragraph(
        "The decision policy (when to take a position) is data, not frozen code. The learning "
        "pass reads the system's own resolved record and proposes nudges, but anti-overfit "
        "guardrails (min-n, max-step) gate every change. <b>Policy v"
        f"{pol.version}</b>; proposals as of {(proposals_doc.get('generated_at') or 'n/a')[:10]}.",
        st["body"]))
    elems.append(_sp(5))

    # Current policy knobs.
    pol_rows = [["Knob (decision threshold)", "Value"],
                ["min profitable EV ($/contract)", f"{pol.min_profitable_ev:.3f}"],
                ["max market-fade gap (no-high-conf)", f"{pol.max_market_disagreement:.3f}"],
                ["conviction medium / high EV", f"{pol.conviction_medium_ev:.2f} / {pol.conviction_high_ev:.2f}"],
                ["low confidence never leans", str(pol.low_confidence_never_leans)],
                ["adversarial veto binding", str(pol.adversarial_veto_binding)]]
    t1 = Table(pol_rows, colWidths=[9.0 * cm, 4.0 * cm])
    t1.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4A148C")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F3E5F5"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CE93D8")),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4)]))
    elems.append(t1)
    elems.append(_sp(6))

    # Pending proposals.
    if proposals:
        elems.append(Paragraph("Pending proposals (from the system's own outcomes)", st["small"]))
        elems.append(_sp(2))
        data = [["Knob", "Current → Proposed", "n", "Guardrail", "Rationale"]]
        for p in proposals:
            data.append([
                p.get("knob", ""),
                f"{p.get('current')} → {p.get('proposed')}",
                str(p.get("n", 0)),
                p.get("guardrail", ""),
                (p.get("rationale", "") or "")[:90],
            ])
        t2 = Table(data, colWidths=[4.2 * cm, 3.2 * cm, 0.9 * cm, 2.6 * cm, 6.1 * cm])
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4A148C")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F3E5F5"), colors.white]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CE93D8")),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 3), ("VALIGN", (0, 0), (-1, -1), "TOP")]
        for i, p in enumerate(proposals, start=1):
            g = p.get("guardrail", "")
            col = (colors.HexColor("#1B5E20") if g == "AUTO_OK"
                   else colors.HexColor("#B71C1C") if g == "HUMAN_GATE"
                   else colors.HexColor("#888888"))
            style.append(("TEXTCOLOR", (3, i), (3, i), col))
        t2.setStyle(TableStyle(style))
        elems.append(t2)
        elems.append(_sp(3))
        elems.append(Paragraph(
            '<font color="#888888">INSUFFICIENT_DATA = directionally real but too few samples to '
            "trust; HUMAN_GATE = change too large to auto-apply; AUTO_OK = guardrail-cleared, "
            "auto-applied. Nothing auto-applies until the record is large enough.</font>",
            st["small"]))
    else:
        elems.append(Paragraph(
            '<font color="#888888">No proposals yet — run scripts/learn_policy.py after '
            "resolutions accumulate.</font>", st["small"]))
    return elems


def _build_per_market(
    watchlist: schemas.Watchlist,
    forecasts: list[schemas.ForecastRecord],
    scratch_dir: Path,
    st: dict,
) -> list:
    """Build per-market sections (one block per active market with a forecast)."""
    elems: list = []
    forecast_map = {f.ticker: f for f in forecasts}
    active = watchlist.active()

    active_with_fc = [m for m in active if m.ticker in forecast_map]
    if not active_with_fc:
        return elems

    # Sort by profitability then absolute edge, descending:
    # markets with a lean come first (highest spot net EV first),
    # then NONE markets by largest absolute edge.
    import math as _math

    def _sort_key(m):
        cur = forecast_map[m.ticker].current
        if cur is None:
            return (0, -_math.inf, 0)
        lean = cur.lean or "NONE"
        has_lean = 1 if lean != "NONE" else 0
        ev = cur.ev_per_contract if cur.ev_per_contract is not None else -_math.inf
        abs_edge = abs(cur.edge) if cur.edge is not None else 0
        return (has_lean, ev, abs_edge)

    active_with_fc = sorted(active_with_fc, key=_sort_key, reverse=True)

    elems.append(Paragraph("Active Markets", st["section"]))
    elems.append(_hr())

    for entry in active_with_fc:
        record = forecast_map[entry.ticker]
        cur = record.current
        block: list = []

        title_text = (
            f'<font size="11"><b>{entry.title or record.title or entry.ticker}</b></font>'
            f'  <font size="8" color="#777777">({entry.ticker})</font>'
        )
        block.append(Paragraph(title_text, st["market_title"]))

        ct = entry.close_time or record.close_time
        close_str = ""
        if ct:
            dt = _parse_dt(ct)
            close_str = dt.strftime("%Y-%m-%d %H:%M UTC") if dt else ct
        cat = entry.category or record.category or "—"
        meta = f'<font color="#555555">Category: {cat}</font>'
        if close_str:
            meta += f'  |  <font color="#555555">Closes: {close_str}</font>'
        block.append(Paragraph(meta, st["small"]))

        if cur:
            edge_val = cur.edge
            lean = cur.lean or "NONE"
            edge_color = _edge_color(edge_val, lean)

            prob_line = (
                f"My prob: <b>{_fmt_pct(cur.my_probability)}</b> &nbsp;"
                f"Confidence: <b>{cur.my_confidence}</b> &nbsp;"
                f"Market: <b>{_fmt_pct(cur.market_implied_probability)}</b> &nbsp;"
                f'Edge: <font color="{edge_color}"><b>{_fmt_pct(edge_val, 1)}</b></font>'
            )
            block.append(Paragraph(prob_line, st["body"]))

            lean_line = f"Lean: <b>{lean}</b>  Conviction: <b>{cur.conviction}</b>"
            block.append(Paragraph(lean_line, st["body"]))

            for prof_para in _profitability_lines(cur, st, close_time=ct or ""):
                block.append(prof_para)

            if cur.rationale_summary:
                block.append(Paragraph(
                    f"<i>Rationale:</i> {cur.rationale_summary}", st["body"]
                ))

            if cur.key_drivers:
                drivers = "; ".join(cur.key_drivers[:5])
                block.append(Paragraph(
                    f"<i>Key drivers:</i> {drivers}", st["small"]
                ))
        else:
            block.append(Paragraph("No forecast yet for this market.", st["small"]))

        # Drift chart
        safe_ticker = entry.ticker.replace("/", "_").replace("\\", "_")
        chart_path = scratch_dir / f"_drift_{safe_ticker}.png"
        saved = _drift_chart(record, chart_path)
        if saved and saved.exists():
            block.append(_sp(3))
            block.append(Image(str(saved), width=11 * cm, height=4.4 * cm))

        block.append(_sp(4))
        block.append(HRFlowable(
            width="100%", thickness=0.3,
            color=colors.HexColor("#E0E0E0"),
            spaceBefore=2, spaceAfter=4,
        ))
        elems.append(KeepTogether(block))

    return elems


def _build_edge_overview(
    watchlist: schemas.Watchlist,
    forecasts: list[schemas.ForecastRecord],
    scratch_dir: Path,
    st: dict,
) -> list:
    """Build edge overview bar chart section."""
    elems: list = []
    forecast_map = {f.ticker: f for f in forecasts}
    active = watchlist.active()

    tickers = []
    edges: list[Optional[float]] = []
    for m in active:
        rec = forecast_map.get(m.ticker)
        if rec and rec.current:
            tickers.append(m.ticker)
            edges.append(rec.current.edge)

    if not tickers:
        return elems

    elems.append(Paragraph("Edge Overview", st["section"]))
    elems.append(_hr())

    chart_path = scratch_dir / "_edge_overview.png"
    saved = _edge_bar_chart(tickers, edges, chart_path)
    if saved and saved.exists():
        # Cap height to fit the page frame (~716pt ≈ 25.3cm); the per-row 0.85cm would
        # otherwise overflow once the watchlist grows past ~28 markets.
        elems.append(
            Image(str(saved),
                  width=13 * cm,
                  height=min(max(3.5, 0.85 * len(tickers)), 22.0) * cm)
        )
    else:
        elems.append(Paragraph("No edge data available.", st["small"]))

    return elems


def _build_profitable_leans(
    watchlist: schemas.Watchlist,
    forecasts: list[schemas.ForecastRecord],
    st: dict,
) -> list:
    """Build 'Profitable Leans (after fees)' summary table section."""
    elems: list = []
    elems.append(Paragraph("Profitable Leans (after fees)", st["section"]))
    elems.append(_hr())

    forecast_map = {f.ticker: f for f in forecasts}
    active = watchlist.active()

    rows: list[tuple] = []
    for m in active:
        rec = forecast_map.get(m.ticker)
        if not rec or not rec.current:
            continue
        cur = rec.current
        lean = cur.lean or "NONE"
        if lean == "NONE":
            continue
        if cur.ev_per_contract is None:
            continue
        rows.append((
            m.ticker,
            lean,
            cur.ev_per_contract,
            cur.ev_limit_per_contract,  # may be None
            cur.conviction or "—",
        ))

    if not rows:
        elems.append(Paragraph(
            "No profitable leans after fees this run — markets are efficiently priced "
            "for our estimates.",
            st["small"],
        ))
        elems.append(_sp(4))
        elems.append(Paragraph(
            "Fees use Kalshi's standard taker formula ceil(0.07*p*(1-p))/contract; "
            "resting (maker) limit fills may be cheaper on some series. "
            "Paper only - read-only key places no orders.",
            st["footnote"],
        ))
        return elems

    # Sort descending by ev_per_contract
    rows.sort(key=lambda r: r[2], reverse=True)

    tbl_data = [["Ticker", "Side", "Spot EV/contract", "Limit EV/contract", "Conviction"]]
    for ticker, side, ev, lev, conv in rows:
        ev_str = _fmt_dollar(ev)
        lev_str = _fmt_dollar(lev) if lev is not None else "—"
        tbl_data.append([ticker, side, ev_str, lev_str, conv])

    col_widths = [3.5 * cm, 1.5 * cm, 3.5 * cm, 3.5 * cm, 2.5 * cm]
    tbl = Table(tbl_data, colWidths=col_widths)

    # Color the EV columns green/red based on sign
    row_styles = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#283593")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.HexColor("#EEF2FF"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C5CAE9")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]
    for i, (_, _, ev, lev, _) in enumerate(rows, start=1):
        ev_color = colors.HexColor("#2E7D32") if ev > 0 else colors.HexColor("#C62828")
        row_styles.append(("TEXTCOLOR", (2, i), (2, i), ev_color))
        row_styles.append(("FONTNAME", (2, i), (2, i), "Helvetica-Bold"))
        if lev is not None:
            lev_color = colors.HexColor("#2E7D32") if lev > 0 else colors.HexColor("#C62828")
            row_styles.append(("TEXTCOLOR", (3, i), (3, i), lev_color))
            row_styles.append(("FONTNAME", (3, i), (3, i), "Helvetica-Bold"))

    tbl.setStyle(TableStyle(row_styles))
    elems.append(tbl)
    elems.append(_sp(4))
    elems.append(Paragraph(
        "Fees use Kalshi's standard taker formula ceil(0.07*p*(1-p))/contract; "
        "resting (maker) limit fills may be cheaper on some series. "
        "Paper only - read-only key places no orders.",
        st["footnote"],
    ))
    return elems


def _build_calibration(
    resolutions: schemas.ResolutionsFile,
    calibration: schemas.Calibration,
    scratch_dir: Path,
    st: dict,
) -> list:
    """Build the calibration section (only when n_resolved >= 1)."""
    elems: list = []

    elems.append(Paragraph("Calibration", st["section"]))
    elems.append(_hr())

    # Reliability diagram
    chart_path = scratch_dir / "_reliability.png"
    saved = _reliability_diagram(calibration, chart_path)
    if saved and saved.exists():
        elems.append(Image(str(saved), width=8 * cm, height=7 * cm))
    else:
        elems.append(Paragraph(
            "Not enough resolved data for reliability diagram.", st["small"]
        ))

    elems.append(_sp(6))

    # Table of resolved markets with brier_mine vs brier_market
    if resolutions.resolved:
        tbl_data = [["Ticker", "Title", "Outcome", "Brier (mine)", "Brier (mkt)"]]
        for r in resolutions.resolved:
            title_trunc = (r.title[:35] + "…") if len(r.title) > 36 else r.title
            outcome_str = "YES" if r.outcome == 1 else "NO"
            # Compute brier scores if not pre-computed
            bm = r.brier_mine
            if bm is None:
                bm = scoring.brier(r.final_my_probability, r.outcome)
            bmkt = r.brier_market
            if bmkt is None and r.final_market_implied is not None:
                bmkt = scoring.brier(r.final_market_implied, r.outcome)
            tbl_data.append([
                r.ticker,
                title_trunc or "—",
                outcome_str,
                _fmt_float(bm),
                _fmt_float(bmkt),
            ])
        tbl = Table(
            tbl_data,
            colWidths=[3.5 * cm, 7 * cm, 2 * cm, 2.5 * cm, 2.5 * cm],
        )
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#283593")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.HexColor("#EEF2FF"), colors.white]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#C5CAE9")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ]))
        elems.append(tbl)

    return elems


def _build_lessons(
    lessons: list[schemas.Lesson],
    st: dict,
) -> list:
    """Build the 'Lessons Learned' section from a list of Lesson records."""
    elems: list = []
    elems.append(Paragraph("Lessons Learned", st["section"]))
    elems.append(_hr())

    if not lessons:
        elems.append(Paragraph(
            '<font color="#888888">No lessons recorded yet — they accrue as markets resolve '
            "and post-mortems run.</font>",
            st["small"],
        ))
        return elems

    # Sort reverse-chronological by created_at; take at most 10
    def _lesson_sort_key(lsn: schemas.Lesson) -> str:
        return lsn.created_at or ""

    recent = sorted(lessons, key=_lesson_sort_key, reverse=True)[:10]

    for lsn in recent:
        block: list = []

        # Main lesson text in bold
        lesson_text = lsn.lesson or "(no lesson text)"
        block.append(Paragraph(f"<b>{lesson_text}</b>", st["body"]))

        # Sub-line: source / ticker / category and pattern_tag
        meta_parts = []
        if lsn.source:
            meta_parts.append(lsn.source)
        if lsn.ticker:
            meta_parts.append(lsn.ticker)
        if lsn.category:
            meta_parts.append(lsn.category)
        meta_str = " &nbsp;|&nbsp; ".join(meta_parts) if meta_parts else ""
        if lsn.pattern_tag:
            tag_str = f'<font color="#1565C0">[{lsn.pattern_tag}]</font>'
            meta_str = f"{meta_str} &nbsp;{tag_str}" if meta_str else tag_str
        if lsn.created_at:
            date_str = lsn.created_at[:10]
            meta_str = f"{date_str} &nbsp;|&nbsp; {meta_str}" if meta_str else date_str
        if meta_str:
            block.append(Paragraph(
                f'<font color="#555555">{meta_str}</font>',
                st["small"],
            ))

        # Brier scores if present
        if lsn.brier_mine is not None or lsn.brier_market is not None:
            brier_parts = []
            if lsn.brier_mine is not None:
                brier_parts.append(f"Brier (mine): {_fmt_float(lsn.brier_mine)}")
            if lsn.brier_market is not None:
                brier_parts.append(f"Brier (market): {_fmt_float(lsn.brier_market)}")
            if lsn.beat_market is not None:
                beat_color = "#2E7D32" if lsn.beat_market else "#C62828"
                beat_label = "beat market" if lsn.beat_market else "lost to market"
                brier_parts.append(
                    f'<font color="{beat_color}">{beat_label}</font>'
                )
            block.append(Paragraph(
                " &nbsp;|&nbsp; ".join(brier_parts),
                st["footnote"],
            ))

        block.append(_sp(3))
        block.append(HRFlowable(
            width="100%", thickness=0.3,
            color=colors.HexColor("#E8EAF6"),
            spaceBefore=2, spaceAfter=4,
        ))
        elems.append(KeepTogether(block))

    return elems


def _build_cost_usage(
    run_log: list[schemas.RunLogEntry],
    scratch_dir: Path,
    st: dict,
) -> list:
    """Build cost & usage section."""
    elems: list = []
    elems.append(Paragraph("Cost &amp; Usage", st["section"]))
    elems.append(_hr())

    recent = run_log[-30:] if run_log else []

    if recent:
        latest = recent[-1]
        u = latest.usage
        detail_data = [
            ["Field", "Value"],
            ["Run ID", latest.run_id or "—"],
            ["Status", latest.status],
            ["Markets researched", str(u.markets_researched)],
            ["Web searches", str(u.web_searches)],
            ["Web fetches", str(u.web_fetches)],
            ["Tool calls", str(u.tool_calls)],
            ["Duration (s)",
             f"{u.duration_s:.1f}" if u.duration_s is not None else "—"],
            ["Est. tokens",
             str(u.est_tokens) if u.est_tokens is not None else "—"],
        ]
        tbl = Table(detail_data, colWidths=[6 * cm, 6 * cm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#37474F")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.HexColor("#ECEFF1"), colors.white]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#B0BEC5")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]))
        elems.append(Paragraph("Latest Run", st["body"]))
        elems.append(_sp(3))
        elems.append(tbl)
        elems.append(_sp(8))

    # Trend chart (needs at least 2 entries)
    if len(recent) >= 2:
        chart_path = scratch_dir / "_usage_trend.png"
        saved = _usage_trend_chart(recent, chart_path)
        if saved and saved.exists():
            elems.append(Paragraph("Usage Trend", st["body"]))
            elems.append(_sp(3))
            elems.append(Image(str(saved), width=12 * cm, height=5.2 * cm))

    elems.append(_sp(8))
    elems.append(Paragraph(
        "Cost proxies; claude.ai/settings/usage is authoritative for tokens/$.",
        st["footnote"],
    ))

    return elems


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def build_pdf(
    watchlist: schemas.Watchlist,
    forecasts: list[schemas.ForecastRecord],
    resolutions: schemas.ResolutionsFile,
    calibration: schemas.Calibration,
    run_log: list[schemas.RunLogEntry],
    lessons: Optional[list[schemas.Lesson]] = None,
    out_path: Path = config.LATEST_PDF_PATH,
) -> Path:
    """Assemble a comprehensive multi-page PDF report and return the output Path.

    Parameters
    ----------
    watchlist:
        Current watchlist (active + historical entries).
    forecasts:
        List of ForecastRecord objects (one per market).
    resolutions:
        All resolved market records.
    calibration:
        Pre-computed calibration statistics; calibration section is gated on
        n_resolved >= 1.
    run_log:
        Agent run log entries (may be long; only last ~30 are used for charts).
    lessons:
        List of Lesson records for the 'Lessons Learned' section.  Defaults to
        None (treated as empty — the section renders a placeholder message).
    out_path:
        Destination PDF path.  Defaults to config.LATEST_PDF_PATH.

    Returns
    -------
    Path
        The path to the written PDF.
    """
    config.ensure_dirs()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    scratch = config.SCRATCH_DIR
    st = _styles()

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN,
        bottomMargin=_MARGIN,
        title="Superforecaster — Kalshi Soft Markets",
    )

    story: list = []

    # 1. Cover / summary
    story.extend(_build_cover(watchlist, forecasts, resolutions, calibration, st))
    story.append(_sp(12))

    # 1a. Structural Edge — THE MONEY PAGE (Workstream A3): the one engine with measured edge
    #     leads the report. Must never block the report from rendering.
    try:
        story.extend(_build_structural_edge(st))
        story.append(_sp(12))
    except Exception:
        pass

    # 1b. Performance over time + per-segment skill (gated on n_resolved >= 1)
    if calibration.n_resolved >= 1:
        story.extend(_build_performance(resolutions, calibration, scratch, st))
        story.append(_sp(12))

    # 1c. Realized Profit & Loss (only renders once a YES/NO lean has resolved)
    pnl_section = _build_profit_and_loss(calibration, st)
    if pnl_section:
        story.extend(pnl_section)
        story.append(_sp(12))

    # 1d. Strategy Scoreboard — which forecasting topology wins (gated on n_resolved >= 1)
    if calibration.n_resolved >= 1:
        scoreboard = _build_strategy_scoreboard(calibration, st)
        if scoreboard:
            story.extend(scoreboard)
            story.append(_sp(12))

    # 1e. Profitability — counterfactual "when do we profit" + forward-looking open EV.
    #     The north-star section: profit is the goal, scored on every forecast.
    prof_section = _build_profitability(calibration, forecasts, st)
    if prof_section:
        story.extend(prof_section)
        story.append(_sp(12))

    # 1f. Autonomous Learning — current policy + the proposals the system generated itself.
    try:
        story.extend(_build_learning(st))
        story.append(_sp(12))
    except Exception:
        pass  # report must always render even if the learning artifacts are absent

    # 2. Per-market section
    story.extend(_build_per_market(watchlist, forecasts, scratch, st))

    # 3. Edge overview
    story.extend(_build_edge_overview(watchlist, forecasts, scratch, st))

    # 4. Profitable leans (after fees)
    story.extend(_build_profitable_leans(watchlist, forecasts, st))

    # 5. Calibration (gated on n_resolved >= 1)
    if calibration.n_resolved >= 1:
        story.extend(_build_calibration(resolutions, calibration, scratch, st))

    # 6. Lessons Learned
    story.extend(_build_lessons(lessons or [], st))

    # 7. Cost & usage
    story.extend(_build_cost_usage(run_log, scratch, st))

    doc.build(story)
    return out_path


# ---------------------------------------------------------------------------
# Self-test  (python3 -m lib.report)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    config.ensure_dirs()

    # ---- synthetic data ------------------------------------------------

    # Market 1: politics
    h1a = schemas.ForecastEntry(
        as_of="2025-03-01T12:00:00Z",
        my_probability=0.65,
        my_confidence="medium",
        market_implied_probability=0.58,
        edge=0.07,
        lean="YES",
        conviction="medium",
        rationale_summary="Strong polling lead; incumbent advantage.",
        key_drivers=["Polling average +5", "Incumbent advantage", "Weak opponent"],
    )
    h1b = schemas.ForecastEntry(
        as_of="2025-03-08T12:00:00Z",
        my_probability=0.70,
        my_confidence="high",
        market_implied_probability=0.62,
        edge=0.08,
        yes_ask=0.63,
        no_ask=0.38,
        fee_per_contract=0.01,
        ev_per_contract=0.13,
        limit_price=0.61,
        ev_limit_per_contract=0.15,
        lean="YES",
        conviction="high",
        rationale_summary="New poll adds to lead; no scandals.",
        key_drivers=["New poll +7", "Economy stable"],
    )
    rec1 = schemas.ForecastRecord(
        ticker="PRES-2025-YES",
        title="Will incumbent win the 2025 election?",
        category="politics",
        close_time="2025-11-04T05:00:00Z",
        current=h1b,
        history=[h1a, h1b],
    )

    # Market 2: economy
    h2a = schemas.ForecastEntry(
        as_of="2025-03-02T10:00:00Z",
        my_probability=0.30,
        my_confidence="low",
        market_implied_probability=None,
        edge=None,
        lean="NO",
        conviction="low",
        rationale_summary="Fed unlikely to cut without clear disinflation.",
        key_drivers=["CPI still elevated", "Powell hawkish"],
    )
    h2b = schemas.ForecastEntry(
        as_of="2025-03-09T10:00:00Z",
        my_probability=0.35,
        my_confidence="medium",
        market_implied_probability=0.40,
        edge=-0.05,
        yes_ask=0.41,
        no_ask=0.60,
        fee_per_contract=0.01,
        ev_per_contract=-0.03,
        lean="NONE",
        conviction="medium",
        rationale_summary="CPI softer, but Fed still cautious.",
        key_drivers=["CPI softer MoM", "Jobs hot"],
    )
    rec2 = schemas.ForecastRecord(
        ticker="FOMC-CUT-MAR",
        title="Will FOMC cut rates in March 2025?",
        category="economy",
        close_time="2025-03-20T18:00:00Z",
        current=h2b,
        history=[h2a, h2b],
    )

    wl = schemas.Watchlist(
        cap=20,
        updated_at=schemas.utc_now_iso(),
        markets=[
            schemas.WatchlistEntry(
                ticker="PRES-2025-YES",
                title="Will incumbent win the 2025 election?",
                category="politics",
                close_time="2025-11-04T05:00:00Z",
                status="active",
            ),
            schemas.WatchlistEntry(
                ticker="FOMC-CUT-MAR",
                title="Will FOMC cut rates in March 2025?",
                category="economy",
                close_time="2025-03-20T18:00:00Z",
                status="active",
            ),
        ],
    )

    res1 = schemas.Resolution(
        ticker="OSCARS-BESTPIC-2025",
        title="Best Picture Oscar 2025",
        category="culture",
        resolved_at="2025-03-10T02:00:00Z",
        outcome=1,
        final_my_probability=0.72,
        final_market_implied=0.68,
        brier_mine=None,
        brier_market=None,
        num_forecasts=3,
    )
    resolutions_file = schemas.ResolutionsFile(
        updated_at=schemas.utc_now_iso(),
        resolved=[res1],
    )

    calibration = scoring.compute_calibration([res1])

    run1 = schemas.RunLogEntry(
        run_id="2025-03-08T06:00:00Z",
        status="complete",
        discovered=3,
        watchlist_size=2,
        reforecast=2,
        resolved_new=0,
        usage=schemas.Usage(
            web_searches=8, web_fetches=12, tool_calls=45,
            markets_researched=2, duration_s=95.3, est_tokens=18000,
        ),
    )
    run2 = schemas.RunLogEntry(
        run_id="2025-03-09T06:00:00Z",
        status="complete",
        discovered=1,
        watchlist_size=2,
        reforecast=2,
        resolved_new=1,
        usage=schemas.Usage(
            web_searches=10, web_fetches=14, tool_calls=52,
            markets_researched=2, duration_s=103.7, est_tokens=21000,
        ),
    )

    # Synthetic lesson
    lesson1 = schemas.Lesson(
        id="2025-03-10T02:00:00Z-OSCARS-BESTPIC-2025",
        created_at="2025-03-10T03:00:00Z",
        source="resolution",
        ticker="OSCARS-BESTPIC-2025",
        category="culture",
        outcome=1,
        brier_mine=0.0784,
        brier_market=0.1024,
        beat_market=True,
        what_went_right="Correctly weighted the frontrunner despite late buzz for the competitor.",
        what_went_wrong="Initial confidence was lower than warranted; took two updates to reach high.",
        lesson="For culture markets with a clear frontrunner, anchor earlier to strong evidence "
               "rather than hedging on contrarian narratives.",
        pattern_tag="culture-frontrunner-underconfidence",
    )

    # ---- full-data report -----------------------------------------------
    test_pdf_path = config.SCRATCH_DIR / "_report_test.pdf"
    result = build_pdf(
        watchlist=wl,
        forecasts=[rec1, rec2],
        resolutions=resolutions_file,
        calibration=calibration,
        run_log=[run1, run2],
        lessons=[lesson1],
        out_path=test_pdf_path,
    )

    assert result.exists(), f"PDF not created: {result}"
    size = result.stat().st_size
    assert size > 1024, f"PDF too small ({size} bytes); expected > 1 KB"

    # ---- empty-input smoke test -----------------------------------------
    empty_pdf = config.SCRATCH_DIR / "_report_empty_test.pdf"
    build_pdf(
        watchlist=schemas.Watchlist(),
        forecasts=[],
        resolutions=schemas.ResolutionsFile(),
        calibration=schemas.Calibration(),
        run_log=[],
        out_path=empty_pdf,
    )
    assert empty_pdf.exists(), "Empty-input PDF not created"
    assert empty_pdf.stat().st_size > 512, "Empty-input PDF too small"

    print("report OK")
