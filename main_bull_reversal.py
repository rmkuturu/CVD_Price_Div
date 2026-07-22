"""
Bull reversal screener: top LOSERS (by chosen lookback) + bullish divergence.

Bull div rule: Price HL, Bybit futures CVD LL; 24 candles per TF
(30m/1h/2h/4h/12h/1d).
"""

import argparse

from screener_core import (
    PERIOD_KEYS,
    RANK_BUCKET_CHOICES,
    TIMEFRAMES_DIV,
    format_entry_watches,
    format_results,
    format_results_csv,
    make_exchange,
    run_screen,
    scan_entry_watches,
)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Top losers + bull divergence (reversal long bias)."
    )
    p.add_argument(
        "--period",
        choices=PERIOD_KEYS,
        default="7d",
        help="Return window to rank losers (1d, 3d, 7d, 30d, 60d).",
    )
    p.add_argument("--top", type=int, default=25, help="How many losers to scan.")
    p.add_argument(
        "--out", type=int, default=20, help="Max symbols in output list."
    )
    p.add_argument(
        "--min-volume",
        type=float,
        default=1_000_000.0,
        help="Min 24h quote volume in USDT (default: 1_000_000).",
    )
    p.add_argument(
        "--qual-pct",
        type=float,
        default=10.0,
        help="Loser qualification: return must be <= -QUAL_PCT%% (default 10).",
    )
    p.add_argument(
        "--no-qual",
        action="store_true",
        help="Disable +/- qualification; rank all movers above min volume.",
    )
    p.add_argument(
        "--universe-scan",
        type=int,
        default=300,
        help="Max symbols (by volume) above min-volume to score.",
    )
    p.add_argument(
        "--bucket",
        choices=RANK_BUCKET_CHOICES,
        default=None,
        help="Optional return bucket override: strong/mild gainers-losers or neutral.",
    )
    p.add_argument(
        "--csv",
        action="store_true",
        help="Print CSV (header row) instead of the ASCII table.",
    )
    p.add_argument(
        "--div-tf",
        nargs="+",
        choices=TIMEFRAMES_DIV,
        default=list(TIMEFRAMES_DIV),
        help="Divergence timeframes to scan. Default: 30m 1h 2h 4h 12h 1d.",
    )
    args = p.parse_args()

    q = args.qual_pct / 100.0
    ex = make_exchange()
    div_timeframes = tuple(args.div_tf)
    rows = run_screen(
        ex,
        period=args.period,
        rank_losers=True,
        div_mode="bull",
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
    qual_note = (
        f"vol>={args.min_volume:,.0f} USDT, bucket={args.bucket}"
        if args.bucket is not None
        else (
            f"vol>={args.min_volume:,.0f} USDT, losers<=-{args.qual_pct:g}%"
            if not args.no_qual
            else f"vol>={args.min_volume:,.0f} USDT, no qual"
        )
    )
    if args.csv:
        print(format_results_csv(rows), end="")
    else:
        print(
            format_results(
                f"BULL REVERSAL | {qual_note} | {args.period} + bull div (Bybit futures CVD)",
                rows,
            )
        )
        print()
        entry_rows = scan_entry_watches(ex, rows, "bull", timeframes=div_timeframes)
        print(format_entry_watches("ENTRY WATCH | next HL after bull div", entry_rows))


if __name__ == "__main__":
    main()
