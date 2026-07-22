"""
Run all four screeners across multiple return windows.

Default periods: 1d, 3d, 7d, 30d.
Bull div rule: Price HL, Bybit futures CVD LL.
Bear div rule: Price LH, Bybit futures CVD HH.
"""

from __future__ import annotations

import argparse
import csv
import io
from dataclasses import dataclass
from typing import Literal

from screener_core import (
    PERIOD_KEYS,
    RANK_BUCKET_CHOICES,
    ScreenResult,
    TIMEFRAMES_DIV,
    make_exchange,
    run_screen,
)


@dataclass(frozen=True)
class ScreenSpec:
    category: str
    rank_losers: bool
    div_mode: Literal["bull", "bear"]


SPECS = (
    ScreenSpec("BULL CONTINUATION", rank_losers=False, div_mode="bull"),
    ScreenSpec("BULL REVERSAL", rank_losers=True, div_mode="bull"),
    ScreenSpec("BEAR CONTINUATION", rank_losers=True, div_mode="bear"),
    ScreenSpec("BEAR REVERSAL", rank_losers=False, div_mode="bear"),
)

DEFAULT_PERIODS = ("1d", "3d", "7d", "30d", "60d")


def _pct_cell(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x * 100:+.2f}"


def _rows_to_table(rows: list[tuple[str, str, int, ScreenResult]]) -> str:
    head = [
        "Category",
        "Period",
        "#",
        "Symbol",
        "1D %",
        "3D %",
        "7D %",
        "30D %",
        "60D %",
        "TFs",
        "Score",
    ]
    body: list[list[str]] = []
    for category, period, rank, result in rows:
        body.append(
            [
                category,
                period,
                str(rank),
                result.symbol,
                _pct_cell(result.returns.get("1d")),
                _pct_cell(result.returns.get("3d")),
                _pct_cell(result.returns.get("7d")),
                _pct_cell(result.returns.get("30d")),
                _pct_cell(result.returns.get("60d")),
                ",".join(result.div_hits),
                str(result.score),
            ]
        )

    widths = [len(h) for h in head]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def sep() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def fmt_row(cells: list[str]) -> str:
        return "|" + "|".join(f" {cells[i]:<{widths[i]}} " for i in range(len(cells))) + "|"

    out = [sep(), fmt_row(head), sep()]
    for row in body:
        out.append(fmt_row(row))
    out.append(sep())
    return "\n".join(out)


def _rows_to_csv(rows: list[tuple[str, str, int, ScreenResult]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        [
            "category",
            "period",
            "rank",
            "symbol",
            "return_1d_pct",
            "return_3d_pct",
            "return_7d_pct",
            "return_30d_pct",
            "return_60d_pct",
            "timeframes",
            "score",
        ]
    )
    for category, period, rank, result in rows:
        def rp(key: str) -> str | float:
            value = result.returns.get(key)
            return "" if value is None else round(value * 100, 4)

        writer.writerow(
            [
                category,
                period,
                rank,
                result.symbol,
                rp("1d"),
                rp("3d"),
                rp("7d"),
                rp("30d"),
                rp("60d"),
                ";".join(result.div_hits),
                result.score,
            ]
        )
    return buf.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run all categories across multiple return windows."
    )
    parser.add_argument(
        "--periods",
        nargs="+",
        choices=PERIOD_KEYS,
        default=list(DEFAULT_PERIODS),
        help="Return windows to scan. Default: 1d 3d 7d 30d 60d.",
    )
    parser.add_argument("--top", type=int, default=25, help="How many movers to scan per category/period.")
    parser.add_argument("--out", type=int, default=10, help="Max rows per category/period.")
    parser.add_argument(
        "--min-volume",
        type=float,
        default=1_000_000.0,
        help="Min 24h quote volume in USDT.",
    )
    parser.add_argument(
        "--qual-pct",
        type=float,
        default=10.0,
        help="Qualification percent for gainers/losers.",
    )
    parser.add_argument(
        "--no-qual",
        action="store_true",
        help="Disable +/- qualification; rank all movers above min volume.",
    )
    parser.add_argument(
        "--universe-scan",
        type=int,
        default=300,
        help="Max symbols by volume above min-volume to score.",
    )
    parser.add_argument(
        "--div-tf",
        nargs="+",
        choices=TIMEFRAMES_DIV,
        default=list(TIMEFRAMES_DIV),
        help="Divergence timeframes to scan. Default: 30m 1h 2h 4h 12h 1d.",
    )
    parser.add_argument(
        "--bucket",
        choices=RANK_BUCKET_CHOICES,
        default=None,
        help="Optional return bucket override: strong/mild gainers-losers or neutral.",
    )
    parser.add_argument("--csv", action="store_true", help="Print CSV instead of table.")
    args = parser.parse_args()

    q = args.qual_pct / 100.0
    exchange = make_exchange()
    div_timeframes = tuple(args.div_tf)
    all_rows: list[tuple[str, str, int, ScreenResult]] = []

    for period in args.periods:
        for spec in SPECS:
            rows = run_screen(
                exchange,
                period=period,
                rank_losers=spec.rank_losers,
                div_mode=spec.div_mode,
                top_universe=args.top,
                out_limit=args.out,
                min_quote_volume_usdt=args.min_volume,
                universe_scan_limit=args.universe_scan,
                gainer_min_return=q,
                loser_max_return=-q,
                use_qualification=not args.no_qual,
                div_timeframes=div_timeframes,
                rank_bucket=args.bucket,
            )
            for rank, row in enumerate(rows, 1):
                all_rows.append((spec.category, period, rank, row))

    if args.csv:
        print(_rows_to_csv(all_rows), end="")
    else:
        qual_note = (
            f"vol>={args.min_volume:,.0f} USDT, bucket={args.bucket}"
            if args.bucket is not None
            else (
                f"vol>={args.min_volume:,.0f} USDT, qual={args.qual_pct:g}%"
                if not args.no_qual
                else f"vol>={args.min_volume:,.0f} USDT, no qual"
            )
        )
        print(f"ALL PERIODS | {qual_note} | periods={','.join(args.periods)}")
        print()
        if all_rows:
            print(_rows_to_table(all_rows))
        else:
            print("(no matches)")


if __name__ == "__main__":
    main()
