"""Write MT5 .set files (UTF-16LE + BOM, the format MetaTrader saves) for EA v7.

The EA's input list is parsed from the .mq5 source itself, so a new input can never
be silently dropped (review finding: the v6.13 writer omitted every v7 input).

Usage:
    python make_sets.py <candidates.json> <presets_dir> [--prefix BG_100USD_] [--hf-account std_raw]
                        [--note "header line"] [--ranges]

candidates.json: {"name": {simulator/EA input: value, ...}}. Simulator-only keys are
translated: EntryTFMin -> EntryTF, SessStartMin/SessEndMin -> SessionStart/End "HH:MM",
SessCloseAtEnd -> CloseAtSessionEnd, NewsFilter -> UseNewsFilter, Hours -> AllowedHours,
MaxHoldMin -> MaxHoldSec. With --hf-account the ladder-fit cost inputs (RiskCommPerLot,
RiskSlipPts) are taken from that simulator account model, so the EA's pre-trade budget
check uses the same costs the optimiser did.
"""
import argparse
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
EA = os.path.join(HERE, "..", "BayesianGrid_EA_v6_PROD.mq5")

ENUM_VALUES = {
    "PERIOD_M1": 1, "PERIOD_M5": 5, "PERIOD_M15": 15, "PERIOD_M30": 30, "PERIOD_H1": 16385,
    "ENTRY_SYMMETRIC": 0, "ENTRY_STRETCH": 1, "NEWS_IMP_HIGH": 3, "NEWS_IMP_MEDIUM": 2,
    "NEWS_CLOSE_NONE": 0, "NEWS_CLOSE_PROFIT": 1, "NEWS_CLOSE_ALL": 2,
}
TF_FROM_MIN = {1: 1, 5: 5, 15: 15, 30: 30, 60: 16385}
SIM_ONLY = {"LotSchedule", "Session", "Ladder", "SpreadScale", "MktSlip", "PathPoints", "PathSeed",
            "UseSpreadProfile", "Balance", "Leverage", "StopOutPct", "ContractSize", "CommPerLotRT", "SwapLong",
            "SwapShort", "SlippagePts", "SpreadPts", "RolloverSpreadMult", "VolMin", "VolStep", "HedgeMarginSum",
            "Point", "EntryTFMin", "SessStartMin", "SessEndMin", "SessCloseAtEnd", "NewsFilter", "Hours",
            "MaxHoldMin"}

# ranges for the MT5 genetic optimiser: name -> (start, step, stop)
OPT_RANGES = {
    "GridSpacingPts": (20, 10, 200), "TP_Pips": (1.0, 0.5, 15.0), "MaxLayers": (1, 1, 6),
    "MaxHoldSec": (30, 30, 1800), "StopPips": (3, 1, 30), "MaxBasketRiskPct": (1, 1, 5),
    "ZEntry": (1.0, 0.25, 3.0), "AddMinSec": (0, 15, 60),
}


def ea_inputs():
    """[(name, type, default_string)] in declaration order, parsed from the EA source."""
    src = open(EA, encoding="utf-8").read()
    out = []
    for m in re.finditer(r"^input\s+(\w+)\s+(\w+)\s*=\s*([^;]+);", src, re.M):
        typ, name, default = m.group(1), m.group(2), m.group(3).strip()
        if typ == "group":
            continue
        out.append((name, typ, default))
    return out


def fmt(typ, v):
    if typ == "bool":
        if isinstance(v, str):
            return v.lower()
        return "true" if v else "false"
    if typ == "string":
        return str(v).strip('"')
    if isinstance(v, str) and v in ENUM_VALUES:
        return str(ENUM_VALUES[v])
    if typ in ("int", "long") or typ.startswith("ENUM_"):
        return str(int(round(float(v))))
    f = float(v)
    return str(int(f)) if f.is_integer() and typ != "double" else repr(f) if not f.is_integer() else f"{f:.1f}"


def hhmm(minutes):
    m = int(minutes)
    return f"{m // 60:02d}:{m % 60:02d}"


def translate(cfg, account=None):
    c = dict(cfg)
    out = {}
    if "EntryTFMin" in c:
        out["EntryTF"] = TF_FROM_MIN[int(c["EntryTFMin"])]
    if c.get("SessStartMin", -1) is not None and float(c.get("SessStartMin", -1)) >= 0:
        out["SessionStart"] = hhmm(c["SessStartMin"])
        out["SessionEnd"] = hhmm(c["SessEndMin"])
    if "SessCloseAtEnd" in c:
        out["CloseAtSessionEnd"] = bool(c["SessCloseAtEnd"])
    if "NewsFilter" in c:
        out["UseNewsFilter"] = bool(c["NewsFilter"])
    if "Hours" in c:
        out["AllowedHours"] = c["Hours"]
    if "MaxHoldMin" in c and float(c["MaxHoldMin"]) > 0 and not c.get("MaxHoldSec"):
        out["MaxHoldSec"] = int(float(c["MaxHoldMin"]) * 60)
    for k, v in c.items():
        if k not in SIM_ONLY:
            out[k] = v
    if account is not None:
        out["RiskCommPerLot"] = account.get("CommPerLotRT", 0.0)
        out["RiskSlipPts"] = account.get("SlippagePts", 2.0)
    return out


def build(cfg, header, account=None, ranges=False):
    inputs = ea_inputs()
    names = {n for n, _, _ in inputs}
    vals = {n: d for n, _, d in inputs}
    tr = translate(cfg, account)
    unknown = sorted(set(tr) - names)
    if unknown:
        raise SystemExit(f"inputs not in the EA: {unknown}")
    vals.update(tr)
    lines = [f"; {h}" for h in header]
    for name, typ, _ in inputs:
        v = fmt(typ, vals[name])
        if ranges and name in OPT_RANGES:
            a, st, b = OPT_RANGES[name]
            lines.append(f"{name}={v}||{a}||{st}||{b}||Y")
        else:
            lines.append(f"{name}={v}")
    return "\r\n".join(lines) + "\r\n"


def write(path, text):
    with open(path, "wb") as f:
        f.write(b"\xff\xfe" + text.encode("utf-16-le"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates")
    ap.add_argument("out")
    ap.add_argument("--prefix", default="BG_100USD_")
    ap.add_argument("--hf-account", default=None)
    ap.add_argument("--note", action="append", default=[])
    ap.add_argument("--ranges", action="store_true", help="also write <prefix>optimize_ranges.set")
    a = ap.parse_args()
    account = None
    if a.hf_account:
        import optimize_hf  # noqa: E402  (account models live there)
        account = optimize_hf.ACCOUNTS[a.hf_account]
    cands = json.load(open(a.candidates))
    os.makedirs(a.out, exist_ok=True)
    for name, cfg in cands.items():
        if a.hf_account and "Session" in cfg:
            import optimize_hf  # noqa: E402
            cfg = optimize_hf.build(cfg)
        hdr = [f"BayesianGrid v7 preset '{name}' (see README.md / HF_GRID_RESEARCH.md)"] + a.note
        write(os.path.join(a.out, f"{a.prefix}{name}.set"), build(cfg, hdr, account))
    if a.ranges:
        base = next(iter(cands.values()))
        if a.hf_account and "Session" in base:
            import optimize_hf  # noqa: E402
            base = optimize_hf.build(base)
        write(os.path.join(a.out, f"{a.prefix}optimize_ranges.set"),
              build(base, ["Ranges for the MT5 genetic optimiser; test with 'Every tick based on real ticks'."],
                    account, ranges=True))
    print("wrote", len(cands) + (1 if a.ranges else 0), "files to", a.out)


if __name__ == "__main__":
    main()
