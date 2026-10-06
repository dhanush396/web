"""Write MT5 .set files (UTF-16LE + BOM, the format MetaTrader saves) for EA v6.13.

Usage: python make_sets.py <candidates.json> <presets_dir>

candidates.json: {"name": {EA input: value, ..., "Hours": "0,1,2"}}
Also writes BG_optimize_ranges.set with ranges for the MT5 genetic optimiser.
"""
import json
import os
import sys

# EA v6.13 inputs in declaration order with their EA defaults (v6.12 live values)
EA_INPUTS = [
    ("BaseLot", 0.08), ("FlatLayers", 5), ("LotIncrement", 0.07), ("LotIncEvery", 1),
    ("GridSpacingPts", 75), ("TP_Pips", 5.3), ("MaxLayers", 18), ("MagicBuy", 519401), ("MagicSell", 519402),
    ("MaxTotalLots", 8.0), ("MarginBufferMult", 5.0), ("MaxSpreadPts", 0), ("MaxEquityDD_Pct", 0.0),
    ("EmergencyCloseDD_Pct", 0.0), ("RefCapital", 0.0), ("ResetHaltOnInit", "true"), ("HaltCooldownMin", 0),
    ("UseNewsFilter", "true"), ("NewsCurrencies", ""), ("NewsMinImportance", 3), ("NewsBeforeMin", 30),
    ("NewsAfterMin", 30), ("NewsPauseLayers", "false"), ("NewsCloseMode", 0), ("NewsCloseMin", 15),
    ("NewsCsvFile", "BG_news_calendar.csv"), ("NewsCsvShiftHours", 0),
    ("CloseOnFriday", "false"), ("FridayCloseHour", 20),
    ("UseTimeFilter", "false"), ("AllowedHours", "2,4,11,13,14,15,16,17,18,21,22"), ("HoursInGMT", "false"),
    ("ServerGMTOffset", 3), ("BlockFriday", "false"),
    ("OpenOnStart", "false"), ("PersistStats", "true"), ("ShowPanel", "true"), ("DebugLog", "true"),
]
BOOLS = {"ResetHaltOnInit", "UseNewsFilter", "NewsPauseLayers", "CloseOnFriday", "UseTimeFilter", "HoursInGMT",
         "BlockFriday", "OpenOnStart", "PersistStats", "ShowPanel", "DebugLog"}
RENAME = {"NewsFilter": "UseNewsFilter", "Hours": "AllowedHours"}

# ranges for the MT5 optimiser: name -> (start, step, stop)
OPT_RANGES = {
    "BaseLot": (0.01, 0.01, 0.05), "GridSpacingPts": (100, 20, 400), "TP_Pips": (10, 2.5, 60),
    "MaxLayers": (6, 1, 18), "EmergencyCloseDD_Pct": (10, 5, 50), "NewsBeforeMin": (30, 30, 120),
    "NewsAfterMin": (30, 30, 120),
}


def fmt(name, v):
    if name in BOOLS:
        if isinstance(v, str):
            return v
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer() and name not in ("BaseLot", "LotIncrement", "TP_Pips"):
        return str(int(v))
    return str(v)


def build(cfg, header, opt=False):
    vals = dict(EA_INPUTS)
    for k, v in cfg.items():
        k = RENAME.get(k, k)
        if k in vals:
            vals[k] = v
    lines = [f"; {h}" for h in header]
    for name, _ in EA_INPUTS:
        v = fmt(name, vals[name])
        if opt and name in OPT_RANGES:
            a, s, b = OPT_RANGES[name]
            lines.append(f"{name}={v}||{a}||{s}||{b}||Y")
        else:
            lines.append(f"{name}={v}")
    return "\r\n".join(lines) + "\r\n"


def write(path, text):
    with open(path, "wb") as f:
        f.write(b"\xff\xfe" + text.encode("utf-16-le"))


def main():
    cands = json.load(open(sys.argv[1]))
    out = sys.argv[2]
    os.makedirs(out, exist_ok=True)
    for name, cfg in cands.items():
        cfg = {k: v for k, v in cfg.items() if k not in ("LotSchedule",)}
        hdr = [f"BayesianGrid v6.13 preset '{name}' for a $100 CENT (USC) EURUSD account = 10,000 USC (see README.md)",
               "Lots are CENT lots. On a standard USD account this preset is NOT valid (0/2400 configs survived $100).",
               "Optimised on Oanda EURUSD M1 2012-2020.05, swap-free model, 1.2 pip spread, 0.2 pip slippage.",
               "Re-validate in the MT5 Strategy Tester ('Every tick based on real ticks') on YOUR broker first."]
        write(os.path.join(out, f"BG_100USD_{name}.set"), build(cfg, hdr))
    base = next(iter(cands.values()))
    write(os.path.join(out, "BG_optimize_ranges.set"),
          build({k: v for k, v in base.items() if k != "LotSchedule"},
                ["Optimisation ranges for the MT5 genetic optimiser (custom max criterion recommended:",
                 "'Balance + max Recovery Factor'); start values = first preset."], opt=True))
    print("wrote", len(cands) + 1, "files to", out)


if __name__ == "__main__":
    main()
