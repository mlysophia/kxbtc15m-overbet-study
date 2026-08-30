"""Reproduce the threshold-0.80 KXBTC15M majority-window study.

The committed signal table is the smallest self-contained research artifact.  Raw
trade and market files are intentionally not part of this repository.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import pandas as pd


THRESHOLD = 0.80
STAKE_DOLLARS = 100.0
FEE_CHANGE_UTC = pd.Timestamp("2026-07-07T00:00:00Z")
DATA_END_UTC = pd.Timestamp("2026-08-19T23:45:00Z")


def extract_signal(
    trades: pd.DataFrame,
    interval_start: pd.Timestamp,
    interval_end: pd.Timestamp,
    window_minutes: int,
    threshold: float = THRESHOLD,
) -> dict[str, object] | None:
    """Apply the historical signal rule to one 15-minute market.

    Prices are treated as piecewise constant between trades.  An observation
    qualifies only when either side is strictly above ``threshold`` for strictly
    more than half of the window.  Entry uses the first trade at or after the
    observation window.  Exact 50/50 entries resolve to YES, matching the archived
    experiment (seven archived rows use that convention).
    """
    if trades.empty:
        return None

    frame = trades.sort_values("created_time").reset_index(drop=True).copy()
    frame["created_time"] = pd.to_datetime(frame["created_time"], utc=True)
    observation_end = interval_start + pd.Timedelta(minutes=window_minutes)

    updates = frame[
        frame["created_time"].ge(interval_start)
        & frame["created_time"].lt(observation_end)
    ].copy()
    prior = frame[frame["created_time"].lt(interval_start)].tail(1).copy()
    if not prior.empty:
        prior.loc[:, "created_time"] = interval_start
        updates = pd.concat([prior, updates], ignore_index=True)
    if updates.empty:
        return None

    next_times = updates["created_time"].shift(-1).fillna(observation_end)
    durations = (next_times - updates["created_time"]).dt.total_seconds().clip(lower=0)
    extreme = updates[["yes_price_prob", "no_price_prob"]].max(axis=1).gt(threshold)
    qualified_seconds = float(durations[extreme].sum())
    if qualified_seconds <= window_minutes * 30:
        return None

    entries = frame[
        frame["created_time"].ge(observation_end)
        & frame["created_time"].le(interval_end)
    ]
    if entries.empty:
        return None
    entry = entries.iloc[0]
    side = "yes" if entry["yes_price_prob"] >= entry["no_price_prob"] else "no"
    return {
        "side": side,
        "entry_time": entry["created_time"],
        "entry_price": float(entry[f"{side}_price_prob"]),
        "entry_trade_count": float(entry["count"]),
        "qualified_seconds": qualified_seconds,
        "observed_seconds": float(durations.sum()),
        "window_minutes": window_minutes,
    }


def taker_fee(contracts: float, price: float, entry_time: pd.Timestamp) -> float:
    """Return the fee used by the archived backtest, including historical rounding."""
    raw_fee = 0.07 * contracts * price * (1 - price)
    increment = 0.0001 if entry_time >= FEE_CHANGE_UTC else 0.01
    return round(math.ceil(raw_fee / increment - 1e-12) * increment, 4)


def add_pnl(
    signals: pd.DataFrame,
    stake: float = STAKE_DOLLARS,
    slippage_cents: int = 0,
) -> pd.DataFrame:
    """Add fixed-stake taker PnL columns without compounding."""
    frame = signals.sort_values("entry_time").copy()
    frame["entry_time"] = pd.to_datetime(frame["entry_time"], utc=True)
    frame["effective_price"] = (
        frame["entry_price"] + slippage_cents / 100
    ).clip(upper=0.9999)
    frame["contracts"] = stake / frame["effective_price"]
    frame["taker_fee"] = frame.apply(
        lambda row: taker_fee(
            row["contracts"], row["effective_price"], row["entry_time"]
        ),
        axis=1,
    )
    frame["pnl"] = (
        frame["won"].astype(int) * frame["contracts"] - stake - frame["taker_fee"]
    )
    frame["capital_deployed"] = stake + frame["taker_fee"]
    return frame


def max_drawdown(pnl: pd.Series) -> float:
    """Return drawdown from the initial zero-equity high-water mark."""
    cumulative = pd.concat(
        [pd.Series([0.0]), pnl.cumsum().reset_index(drop=True)], ignore_index=True
    )
    drawdown = cumulative - cumulative.cummax()
    return float(drawdown.min())


def summarize_ladder(
    signals: pd.DataFrame, stake: float = STAKE_DOLLARS
) -> pd.DataFrame:
    """Build the complete 1-10 minute fill/slippage sensitivity table."""
    rows: list[dict[str, object]] = []
    for window_minutes in range(1, 11):
        for fill_model in ("next_trade_taker", "observed_trade_capacity"):
            for slippage_cents in (0, 1, 2):
                sample = add_pnl(
                    signals[signals["window_minutes"] == window_minutes],
                    stake,
                    slippage_cents,
                )
                if fill_model == "observed_trade_capacity":
                    sample = sample[
                        sample["entry_trade_count"] >= sample["contracts"]
                    ].copy()
                count = len(sample)
                deployed = float(sample["capital_deployed"].sum())
                rows.append(
                    {
                        "window_minutes": window_minutes,
                        "fill_model": fill_model,
                        "slippage_cents": slippage_cents,
                        "trades": count,
                        "wins": int(sample["won"].sum()) if count else 0,
                        "win_rate": float(sample["won"].mean()) if count else None,
                        "avg_entry_price": (
                            float(sample["entry_price"].mean()) if count else None
                        ),
                        "taker_fees": float(sample["taker_fee"].sum()),
                        "net_pnl": float(sample["pnl"].sum()),
                        "capital_deployed": deployed,
                        "net_roi": (
                            float(sample["pnl"].sum() / deployed) if deployed else None
                        ),
                        "max_drawdown": max_drawdown(sample["pnl"]) if count else 0.0,
                    }
                )
    return pd.DataFrame(rows)


def _period_row(
    signals: pd.DataFrame,
    window_minutes: int,
    period: str,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> dict[str, object]:
    sample = signals[signals["window_minutes"] == window_minutes].copy()
    sample["entry_time"] = pd.to_datetime(sample["entry_time"], utc=True)
    sample["close_time"] = pd.to_datetime(sample["close_time"], utc=True)
    if start is not None:
        sample = sample[sample["close_time"].ge(start)]
    if end is not None:
        sample = sample[sample["close_time"].lt(end)]
    sample = add_pnl(sample)
    deployed = float(sample["capital_deployed"].sum())
    return {
        "window_minutes": window_minutes,
        "period": period,
        "first_market_close_utc": sample["close_time"].min().isoformat(),
        "last_market_close_utc": sample["close_time"].max().isoformat(),
        "trades": len(sample),
        "wins": int(sample["won"].sum()),
        "win_rate": float(sample["won"].mean()),
        "avg_entry_price": float(sample["entry_price"].mean()),
        "realized_minus_entry_probability": float(
            sample["won"].astype(int).mean() - sample["entry_price"].mean()
        ),
        "taker_fees": float(sample["taker_fee"].sum()),
        "net_pnl": float(sample["pnl"].sum()),
        "capital_deployed": deployed,
        "net_roi": float(sample["pnl"].sum() / deployed),
        "max_drawdown": max_drawdown(sample["pnl"]),
    }


def key_results(signals: pd.DataFrame) -> pd.DataFrame:
    """Return the headline and explicitly descriptive regime slices."""
    latest_start = DATA_END_UTC - pd.Timedelta(days=30)
    mar_1 = pd.Timestamp("2026-03-01T00:00:00Z")
    jun_1 = pd.Timestamp("2026-06-01T00:00:00Z")
    rows = [
        _period_row(signals, 5, "full_sample"),
        _period_row(signals, 10, "full_sample"),
        _period_row(
            signals,
            10,
            "available_2025_slice",
            end=pd.Timestamp("2026-01-01T00:00:00Z"),
        ),
        _period_row(signals, 10, "early_through_feb_2026", end=mar_1),
        _period_row(signals, 10, "reversal_mar_may_2026", start=mar_1, end=jun_1),
        _period_row(
            signals,
            10,
            "rebound_jun_to_latest_30d",
            start=jun_1,
            end=latest_start,
        ),
        _period_row(signals, 10, "latest_30d", start=latest_start),
    ]
    return pd.DataFrame(rows)


def plot_ladder(signals: pd.DataFrame, output: Path) -> None:
    """Plot cumulative PnL with every triggered contract over the full sample."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    fig, axes = plt.subplots(5, 2, figsize=(16, 20), sharex=True)
    for window, axis in zip(range(1, 11), axes.flat):
        frame = add_pnl(signals[signals["window_minutes"] == window])
        frame["cumulative_pnl"] = frame["pnl"].cumsum()
        axis.plot(
            frame["entry_time"], frame["cumulative_pnl"], linewidth=1.1, color="tab:blue"
        )
        wins = frame[frame["won"]]
        losses = frame[~frame["won"]]
        axis.scatter(
            wins["entry_time"],
            wins["cumulative_pnl"],
            s=9,
            color="tab:green",
            alpha=0.55,
            linewidths=0,
            label="winning trigger",
            rasterized=True,
        )
        axis.scatter(
            losses["entry_time"],
            losses["cumulative_pnl"],
            s=16,
            color="tab:red",
            alpha=0.8,
            marker="x",
            linewidths=0.7,
            label="losing trigger",
            rasterized=True,
        )
        axis.axhline(0, color="black", linewidth=0.7, alpha=0.6)
        final = frame["cumulative_pnl"].iloc[-1] if len(frame) else 0
        axis.set_title(f"{window} min | {len(frame):,} trades | final ${final:,.0f}")
        axis.grid(alpha=0.25)
        axis.yaxis.set_major_formatter(
            FuncFormatter(
                lambda value, _: (
                    f"${value / 1000:.1f}k" if abs(value) >= 1000 else f"${value:,.0f}"
                )
            )
        )
        axis.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    axes.flat[0].legend(loc="best", fontsize=8)
    fig.suptitle(
        "KXBTC15M cumulative net PnL and triggered contracts — threshold 0.80\n"
        "$100 per trade; taker fees included; no added slippage",
        fontsize=16,
    )
    fig.supxlabel("Entry date")
    fig.supylabel("Cumulative net PnL")
    fig.tight_layout(rect=(0.03, 0.03, 1, 0.96))
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=170, bbox_inches="tight")
    plt.close(fig)


def verify_summary(actual: pd.DataFrame, expected_path: Path) -> None:
    expected = pd.read_csv(expected_path)
    pd.testing.assert_frame_equal(
        actual.reset_index(drop=True),
        expected.reset_index(drop=True),
        check_exact=False,
        rtol=1e-11,
        atol=1e-8,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--signals",
        type=Path,
        default=Path("results/threshold_080_signals.parquet"),
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=Path("results/threshold_080_ladder_summary.csv"),
    )
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    signals = pd.read_parquet(args.signals)
    summary = summarize_ladder(signals)
    if args.verify:
        verify_summary(summary, args.summary)
        print(f"verified {len(summary)} published ladder rows")
    if args.write:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(args.summary, index=False)
        key_path = args.summary.parent / "threshold_080_key_results.csv"
        key_results(signals).to_csv(key_path, index=False)
        figure = Path("figures/threshold_080_pnl_and_triggers.png")
        plot_ladder(signals, figure)
        print(f"wrote {args.summary}, {key_path}, and {figure}")
    if not args.verify and not args.write:
        print(key_results(signals).to_string(index=False))


if __name__ == "__main__":
    main()
