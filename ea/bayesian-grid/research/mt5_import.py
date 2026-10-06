"""Convert MetaTrader 5 exports into the simulator's .npz format.

Bars  (MT5: View -> Symbols -> Bars -> pick M1 -> Export):
    <DATE>	<TIME>	<OPEN>	<HIGH>	<LOW>	<CLOSE>	<TICKVOL>	<VOL>	<SPREAD>
Ticks (MT5: View -> Symbols -> Ticks -> Export):
    <DATE>	<TIME>	<BID>	<ASK>	<LAST>	<VOLUME>	<FLAGS>

MT5 timestamps are already broker SERVER time, which is what the simulator uses.
Bar exports carry BID prices plus the bar's spread in points; they are converted to
mid = bid + spread/2 so the simulator's mid +/- half-spread model reproduces the
broker's bid/ask. Tick exports are resampled to 1-minute mid OHLC with the bar's
mean spread (points).

Usage:
    python mt5_import.py bars  <export.csv> <out.npz> --point 0.01
    python mt5_import.py ticks <export.csv> <out.npz> --point 0.01
--point is the symbol's point size (EURUSD 0.00001, XAUUSD 0.01, US30 1.0 ...).
Remember to set ContractSize for non-FX symbols when you backtest (XAUUSD = 100).
"""
import argparse

import numpy as np
import pandas as pd


def _read(path):
    df = pd.read_csv(path, sep=None, engine="python")
    df.columns = [c.strip("<>").upper() for c in df.columns]
    return df


def _stamp(df):
    ts = pd.to_datetime(df["DATE"].astype(str) + " " + df["TIME"].astype(str), format="mixed")
    return ts


def _seconds(ts):
    return ((ts - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy().astype(np.int64)


def from_bars(path, point):
    df = _read(path)
    ts = _stamp(df)
    spread = df["SPREAD"].astype(float).to_numpy() if "SPREAD" in df else np.zeros(len(df))
    half = spread * point / 2.0
    o, h, l, c = (df[k].astype(float).to_numpy() + half for k in ("OPEN", "HIGH", "LOW", "CLOSE"))
    return ts, o, h, l, c, spread


def from_ticks(path, point):
    df = _read(path)
    ts = _stamp(df)
    q = pd.DataFrame({"bid": pd.to_numeric(df["BID"], errors="coerce").to_numpy(),
                      "ask": pd.to_numeric(df["ASK"], errors="coerce").to_numpy()},
                     index=pd.DatetimeIndex(ts)).ffill().dropna()  # empty fields = unchanged side
    q = q[q.ask >= q.bid]
    mid = (q.bid + q.ask) / 2.0
    spr = (q.ask - q.bid) / point
    bars = mid.resample("1min").ohlc().dropna()
    spread = spr.resample("1min").mean().reindex(bars.index).ffill()
    return (bars.index.to_series(), bars["open"].to_numpy(), bars["high"].to_numpy(), bars["low"].to_numpy(),
            bars["close"].to_numpy(), spread.to_numpy())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["bars", "ticks"])
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--point", type=float, required=True)
    a = ap.parse_args()
    ts, o, h, l, c, spread = (from_bars if a.kind == "bars" else from_ticks)(a.src, a.point)
    t = _seconds(pd.Series(ts).reset_index(drop=True))
    order = np.argsort(t, kind="stable")
    t, o, h, l, c, spread = (x[order] for x in (t, o, h, l, c, spread))
    h = np.maximum.reduce([h, o, c])
    l = np.minimum.reduce([l, o, c])
    np.savez_compressed(a.out, t_srv=t, t_utc=t, o=o, h=h, l=l, c=c, spread=spread, point=np.float64(a.point))
    print(f"{len(t):,} bars {pd.Timestamp(t[0], unit='s')} -> {pd.Timestamp(t[-1], unit='s')}  "
          f"median spread {np.median(spread):.1f} pts -> {a.out}")


if __name__ == "__main__":
    main()
