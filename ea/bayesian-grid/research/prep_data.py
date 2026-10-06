"""Convert Oanda M1 OHLC CSVs (UTC, mid prices) into a single compressed .npz
in broker SERVER time (NY-close convention: GMT+2 winter / GMT+3 US-summer,
which is what Equiti / Vantage / IC / Pepperstone MT5 servers use).

Usage:
    python prep_data.py <oanda_EUR_USD_dir> <out.npz> [first_year] [last_year]

Input layout (as in FutureSharks/financial-data):
    <dir>/<year>/oanda-EUR_USD-<year>-<month>.csv  with header
    time,close,high,low,open,volume
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def load(src: Path, y0: int, y1: int) -> pd.DataFrame:
    frames = []
    for y in range(y0, y1 + 1):
        ydir = src / str(y)
        if not ydir.is_dir():
            continue
        for f in sorted(ydir.glob("*.csv")):
            df = pd.read_csv(f, usecols=["time", "open", "high", "low", "close", "volume"])
            frames.append(df)
    if not frames:
        raise SystemExit(f"no csv files under {src}")
    df = pd.concat(frames, ignore_index=True)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    # sanity: high >= max(open, close), low <= min(open, close)
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)
    return df


def to_server_seconds(utc: pd.Series) -> np.ndarray:
    # NY-close server time == New York local time + 7h (handles US DST exactly)
    ny = utc.dt.tz_convert("America/New_York").dt.tz_localize(None)
    srv = ny + pd.Timedelta(hours=7)
    return ((srv - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy()


def main():
    src = Path(sys.argv[1])
    out = Path(sys.argv[2])
    y0 = int(sys.argv[3]) if len(sys.argv) > 3 else 2012
    y1 = int(sys.argv[4]) if len(sys.argv) > 4 else 2020
    df = load(src, y0, y1)
    t_srv = to_server_seconds(df["time"])
    t_utc = ((df["time"] - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)).to_numpy()
    np.savez_compressed(
        out,
        t_srv=t_srv.astype(np.int64),
        t_utc=t_utc.astype(np.int64),
        o=df["open"].to_numpy(),
        h=df["high"].to_numpy(),
        l=df["low"].to_numpy(),
        c=df["close"].to_numpy(),
        v=df["volume"].to_numpy().astype(np.int64),
    )
    print(f"bars={len(df):,}  {df['time'].iloc[0]} -> {df['time'].iloc[-1]}  -> {out}")


if __name__ == "__main__":
    main()
