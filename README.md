# CVD Price Divergence Screener

A Python screener for finding futures markets where price structure and cumulative volume delta (CVD) disagree.

The project scans liquid USDT perpetuals, ranks coins by recent return, then checks whether price is making a higher low or lower high while futures CVD is making the opposite move. That mismatch is used as a continuation or reversal signal depending on which return bucket the asset came from.

## Core Concept

The screener combines two ideas:

1. Relative strength or weakness.
2. Price/CVD divergence.

The flow is:

1. Build a universe of liquid USDT perpetual futures.
2. Rank symbols by return over a selected period like `1d`, `3d`, `7d`, `30d`, or `60d`.
3. Keep only the symbols that match the chosen context:
   - bull continuation: strong gainers
   - bull reversal: strong losers
   - bear continuation: strong losers
   - bear reversal: strong gainers
4. Check multiple intraday and daily timeframes for price/CVD divergence.
5. Score symbols by how many timeframes agree.
6. Optionally mark entry-watch setups after the divergence has already reacted.

## What CVD Means Here

CVD measures net aggressive buying versus selling over time.

In Coinalyze mode, each candle has total volume and buy volume.

Formula:

```text
sell_volume = total_volume - buy_volume
delta = buy_volume - sell_volume
cvd = cumulative_sum(delta)
```

That is equivalent to:

```text
delta = 2 * buy_volume - total_volume
```

In Binance futures mode, the code uses taker-buy base volume from futures klines:

```text
delta = 2 * taker_buy_base - total_volume
cvd = cumulative_sum(delta)
```

In fallback Bybit mode, true buy/sell split is not available, so the code uses signed volume:

```text
direction = sign(close - previous_close)
cvd = cumulative_sum(volume * direction)
```

Coinalyze is the default and preferred source.

## Main Formulas

### Period Return

The project ranks symbols with simple percentage return:

```text
return(period) = current_close / past_close - 1
```

Examples used in the code:

- `1d`
- `3d`
- `7d`
- `30d`
- `60d`

### Smoothed CVD Pressure

Before checking divergence, raw CVD is smoothed with an EMA:

```text
pressure = EMA(cvd, span=3)
```

This is the series compared against price pivots.

### Bullish Divergence Rule

The current implementation defines bullish divergence as:

```text
price makes a higher low
AND
CVD pressure makes a lower low
```

Code logic:

```text
price_low_2 > price_low_1
AND
cvd_2 < cvd_1
```

The screener then requires a minimum price swing and a minimum CVD divergence strength relative to the recent CVD range.

### Bearish Divergence Rule

The current implementation defines bearish divergence as:

```text
price makes a lower high
AND
CVD pressure makes a higher high
```

Code logic:

```text
price_high_2 < price_high_1
AND
cvd_2 > cvd_1
```

### Divergence Strength Filter

A divergence is only accepted if it is large enough to matter.

Important thresholds from the code:

- minimum price swing fraction: `0.0005` = `0.05%`
- minimum CVD divergence fraction of recent range: `0.02` = `2%`
- CVD EMA span: `3`
- candles checked per timeframe: `24`
- pivot shape: `left=2`, `right=2`

Conceptually:

```text
CVD divergence size / recent CVD range >= 2%
```

## How the Four Modes Work

### Bull Continuation

- rank recent gainers
- look for bullish divergence
- idea: strong trend, temporary internal absorption, continuation setup

### Bull Reversal

- rank recent losers
- look for bullish divergence
- idea: weak market may be bottoming while sell pressure loses conviction

### Bear Continuation

- rank recent losers
- look for bearish divergence
- idea: weak trend, temporary rebound in internals, continuation lower

### Bear Reversal

- rank recent gainers
- look for bearish divergence
- idea: strong market may be topping while buy pressure becomes inefficient

## Entry-Watch Logic

After a valid divergence is found, the project can label whether price has reacted enough and whether it is pulling back toward a possible next entry area.

Important thresholds:

- minimum reaction after pivot: `1.0%`
- near-pivot threshold: `1.5%`
- pullback threshold: `4.0%`

Bull-side statuses:

- `near next HL`
- `pullback forming`
- `waiting pullback`

Bear-side statuses:

- `near next LH`
- `pullback forming`
- `waiting pullback`

## Data Universe

The screener focuses on liquid USDT perpetual futures.

Default filters:

- universe scan limit: `300`
- minimum quote volume: `1,000,000 USDT`

Default divergence timeframes:

- `30m`
- `1h`
- `2h`
- `4h`
- `12h`
- `1d`

## Project Files

- `screener_core.py`: all ranking, return, CVD, pivot, divergence, scoring, and formatting logic
- `dashboard.py`: Tkinter desktop dashboard
- `main_bull_continuation.py`: CLI for bull continuation scans
- `main_bull_reversal.py`: CLI for bull reversal scans
- `main_bear_continuation.py`: CLI for bear continuation scans
- `main_bear_reversal.py`: CLI for bear reversal scans
- `main_all_periods.py`: runs all four categories across multiple periods
- `main.py`: default entry point
- `open_dashboard.bat`: quick launcher for the dashboard on Windows

## Installation

```bash
pip install -r requirements.txt
```

You also need a Coinalyze API key for the default mode.

Set it with an environment variable:

```powershell
$env:COINALYZE_API_KEY="your_api_key"
```

Or keep it locally in:

- `.dashboard_config.json`

Example format:

```json
{
  "coinalyze_api_key": "your_api_key"
}
```

## Usage

### Launch the Dashboard

```powershell
python dashboard.py
```

Or:

```powershell
.\open_dashboard.bat
```

### Run One Screener

Example bull continuation scan:

```powershell
python main_bull_continuation.py --period 7d --top 25 --out 20 --min-volume 1000000 --qual-pct 10
```

Example bear reversal scan:

```powershell
python main_bear_reversal.py --period 30d --top 25 --out 20
```

### Run All Categories Across Periods

```powershell
python main_all_periods.py --periods 1d 3d 7d 30d 60d --top 25 --out 10
```

### Export CSV

```powershell
python main_all_periods.py --csv
```

## Output Interpretation

Each row includes:

- symbol
- multi-period returns
- divergence timeframes hit
- score = number of timeframes confirming the setup

A higher score means more timeframe agreement, not guaranteed trade quality.

## Notes

- Default source is Coinalyze.
- Default exchange universe is Bybit USDT perpetuals.
- SSL verification can be disabled with `SCREENER_SSL_NO_VERIFY=1` if a network environment forces it.
- The reference docs and local config files are intentionally kept out of GitHub but can remain in your local workspace.
