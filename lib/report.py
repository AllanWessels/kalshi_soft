"""PDF report generator for the kalshi_soft superforecaster.

Assembles a clean, multi-page PDF from watchlist data, forecast records,
resolutions, calibration, and run-log entries.  Uses matplotlib for charts
(non-interactive Agg backend) and reportlab.platypus for page layout.

Public API
----------
build_pdf(watchlist, forecasts, resolutions, calibration, run_log,
          out_path=config.LATEST_PDF_PATH) -> Path
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

from lib import schemas, config, scoring

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


def _profitability_line(cur: schemas.ForecastEntry, st: dict) -> Optional[object]:
    """Return a Paragraph for the fee-aware profitability line, or None if no ev data."""
    if cur.ev_per_contract is None:
        return None

    ev = cur.ev_per_contract
    lean = cur.lean or "NONE"
    fee = cur.fee_per_contract  # may be None for older records

    if lean == "NONE":
        # No profitable lean: show best net EV
        ev_str = _fmt_dollar(ev)
        text = f"Profitability: no profitable edge after fees (best net EV {ev_str}/contract)"
        color = "#888888"
    else:
        # Determine the relevant ask price
        if lean == "YES":
            ask = cur.yes_ask
        else:
            ask = cur.no_ask

        ev_str = _fmt_dollar(ev)
        color = "#2E7D32" if ev > 0 else "#C62828"

        # Build the detail suffix
        parts: list[str] = []
        if fee is not None:
            parts.append(f"fee ${fee:.2f}")
        if ask is not None:
            parts.append(f"ask ${ask:.2f}")
        detail = ", ".join(parts)
        detail_str = f" ({detail})" if detail else ""
        conv = cur.conviction or "—"
        text = (
            f'Profitability: {lean} — net EV <font color="{color}"><b>{ev_str}/contract</b></font>'
            f"{detail_str}, conviction {conv}"
        )

    return Paragraph(text, st["body"])


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

            prof_para = _profitability_line(cur, st)
            if prof_para is not None:
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
        elems.append(
            Image(str(saved),
                  width=13 * cm,
                  height=max(3.5, 0.85 * len(tickers)) * cm)
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
        title = m.title or rec.title or m.ticker
        title_trunc = (title[:38] + "…") if len(title) > 39 else title
        rows.append((
            m.ticker,
            title_trunc,
            lean,
            cur.ev_per_contract,
            cur.conviction or "—",
        ))

    if not rows:
        elems.append(Paragraph(
            "No profitable leans after fees this run — markets are efficiently priced "
            "for our estimates.",
            st["small"],
        ))
        return elems

    # Sort descending by ev_per_contract
    rows.sort(key=lambda r: r[3], reverse=True)

    tbl_data = [["Ticker", "Title", "Side", "Net EV/contract", "Conviction"]]
    for ticker, title_trunc, side, ev, conv in rows:
        ev_str = _fmt_dollar(ev)
        tbl_data.append([ticker, title_trunc, side, ev_str, conv])

    col_widths = [3.5 * cm, 7.5 * cm, 1.5 * cm, 3.0 * cm, 2.5 * cm]
    tbl = Table(tbl_data, colWidths=col_widths)

    # Color the Net EV column green/red based on sign
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
    for i, (_, _, _, ev, _) in enumerate(rows, start=1):
        ev_color = colors.HexColor("#2E7D32") if ev > 0 else colors.HexColor("#C62828")
        row_styles.append(("TEXTCOLOR", (3, i), (3, i), ev_color))
        row_styles.append(("FONTNAME", (3, i), (3, i), "Helvetica-Bold"))

    tbl.setStyle(TableStyle(row_styles))
    elems.append(tbl)
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
            ["Duration (s)", f"{u.duration_s:.1f}"],
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

    # 2. Per-market section
    story.extend(_build_per_market(watchlist, forecasts, scratch, st))

    # 3. Edge overview
    story.extend(_build_edge_overview(watchlist, forecasts, scratch, st))

    # 4. Profitable leans (after fees)
    story.extend(_build_profitable_leans(watchlist, forecasts, st))

    # 5. Calibration (gated on n_resolved >= 1)
    if calibration.n_resolved >= 1:
        story.extend(_build_calibration(resolutions, calibration, scratch, st))

    # 6. Cost & usage
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

    # ---- full-data report -----------------------------------------------
    test_pdf_path = config.SCRATCH_DIR / "_report_test.pdf"
    result = build_pdf(
        watchlist=wl,
        forecasts=[rec1, rec2],
        resolutions=resolutions_file,
        calibration=calibration,
        run_log=[run1, run2],
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
