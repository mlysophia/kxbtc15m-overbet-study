from pathlib import Path
import sys
import unittest

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from study import (  # noqa: E402
    extract_signal,
    max_drawdown,
    summarize_ladder,
    taker_fee,
    verify_summary,
)


class StudyTests(unittest.TestCase):
    def test_signal_requires_strict_time_majority(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        trades = pd.DataFrame(
            {
                "created_time": pd.to_datetime(
                    [
                        "2026-01-01T00:00:00Z",
                        "2026-01-01T00:00:31Z",
                        "2026-01-01T00:01:00Z",
                    ]
                ),
                "yes_price_prob": [0.85, 0.70, 0.70],
                "no_price_prob": [0.15, 0.30, 0.30],
                "count": [10.0, 10.0, 200.0],
            }
        )
        signal = extract_signal(
            trades,
            start,
            start + pd.Timedelta(minutes=15),
            window_minutes=1,
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["side"], "yes")
        self.assertEqual(signal["entry_price"], 0.70)
        self.assertEqual(signal["qualified_seconds"], 31.0)

        trades.loc[1, "created_time"] = pd.Timestamp("2026-01-01T00:00:30Z")
        self.assertIsNone(
            extract_signal(
                trades,
                start,
                start + pd.Timedelta(minutes=15),
                window_minutes=1,
            )
        )

    def test_threshold_is_strict_and_fee_rounding_changes_on_schedule(self) -> None:
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        trades = pd.DataFrame(
            {
                "created_time": pd.to_datetime(
                    ["2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"]
                ),
                "yes_price_prob": [0.80, 0.80],
                "no_price_prob": [0.20, 0.20],
                "count": [10.0, 200.0],
            }
        )
        self.assertIsNone(
            extract_signal(
                trades,
                start,
                start + pd.Timedelta(minutes=15),
                window_minutes=1,
            )
        )
        self.assertEqual(taker_fee(1, 0.8, pd.Timestamp("2026-01-01T00:00:00Z")), 0.02)
        self.assertEqual(taker_fee(1, 0.8, pd.Timestamp("2026-08-01T00:00:00Z")), 0.0112)

    def test_recalculated_summary_matches_published_csv(self) -> None:
        signals = pd.read_parquet(ROOT / "results/threshold_080_signals.parquet")
        actual = summarize_ladder(signals)
        verify_summary(actual, ROOT / "results/threshold_080_ladder_summary.csv")

    def test_drawdown_starts_from_zero_equity(self) -> None:
        self.assertEqual(max_drawdown(pd.Series([-100.0, 20.0])), -100.0)


if __name__ == "__main__":
    unittest.main()
