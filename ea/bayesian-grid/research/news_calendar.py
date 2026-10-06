"""Rule-based HIGH-impact USD/EUR news calendar for backtesting.

Why rule-based: the MQL5 Economic Calendar is not available inside the MT5
Strategy Tester, and this research box has no calendar feed. So the backtest
uses events whose timing is either published years in advance or follows a
fixed rule. Live trading uses the full MQL5 calendar (many more events), so
the live filter blocks MORE time than the backtest -> backtest news numbers
are a slightly optimistic proxy for opportunity cost, conservative for risk.

Events:
  - US Non-Farm Payrolls (BLS rule: 3rd Friday after the week containing
    the 12th; 08:30 New York)
  - FOMC statements (published meeting calendar; 14:00 New York, 2012 had
    12:30 / 14:15 variants)
  - ECB monetary-policy decision + press conference (published calendar;
    13:45/14:30 CET until Jun-2022, 14:15/14:45 CET after)
  - ISM Manufacturing PMI (1st US business day, 10:00 New York)

Output CSV (for the EA's tester fallback) is in broker SERVER time using the
NY-close convention (GMT+2 winter / GMT+3 summer):
    YYYY.MM.DD HH:MI,CUR,IMPORTANCE,TITLE

Usage:  python news_calendar.py <out.csv> [first_year] [last_year]
"""
import datetime as dt
import sys
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
CET = ZoneInfo("Europe/Berlin")
UTC = dt.timezone.utc

FOMC = {
    2012: ["01-25", "03-13", "04-25", "06-20", "08-01", "09-13", "10-24", "12-12"],
    2013: ["01-30", "03-20", "05-01", "06-19", "07-31", "09-18", "10-30", "12-18"],
    2014: ["01-29", "03-19", "04-30", "06-18", "07-30", "09-17", "10-29", "12-17"],
    2015: ["01-28", "03-18", "04-29", "06-17", "07-29", "09-17", "10-28", "12-16"],
    2016: ["01-27", "03-16", "04-27", "06-15", "07-27", "09-21", "11-02", "12-14"],
    2017: ["02-01", "03-15", "05-03", "06-14", "07-26", "09-20", "11-01", "12-13"],
    2018: ["01-31", "03-21", "05-02", "06-13", "08-01", "09-26", "11-08", "12-19"],
    2019: ["01-30", "03-20", "05-01", "06-19", "07-31", "09-18", "10-30", "12-11"],
    2020: ["01-29", "04-29", "06-10", "07-29", "09-16", "11-05", "12-16"],
    2021: ["01-27", "03-17", "04-28", "06-16", "07-28", "09-22", "11-03", "12-15"],
    2022: ["01-26", "03-16", "05-04", "06-15", "07-27", "09-21", "11-02", "12-14"],
    2023: ["02-01", "03-22", "05-03", "06-14", "07-26", "09-20", "11-01", "12-13"],
    2024: ["01-31", "03-20", "05-01", "06-12", "07-31", "09-18", "11-07", "12-18"],
    2025: ["01-29", "03-19", "05-07", "06-18", "07-30", "09-17", "10-29", "12-10"],
    2026: ["01-28", "03-18", "04-29", "06-17", "07-29", "09-16", "10-28", "12-09"],
}
# 2012 statement times differed (press-conference meetings released 12:30)
FOMC_2012_1230 = {"01-25", "04-25", "06-20", "09-13", "12-12"}
# 2020 emergency moves (unscheduled, but real): 03-03 10:00 NY, 03-15 17:00 NY (Sunday)
FOMC_EXTRA = [("2020-03-03", "10:00"), ("2020-03-15", "17:00")]

ECB = {
    2012: ["01-12", "02-09", "03-08", "04-04", "05-03", "06-06", "07-05", "08-02", "09-06", "10-04", "11-08", "12-06"],
    2013: ["01-10", "02-07", "03-07", "04-04", "05-02", "06-06", "07-04", "08-01", "09-05", "10-02", "11-07", "12-05"],
    2014: ["01-09", "02-06", "03-06", "04-03", "05-08", "06-05", "07-03", "08-07", "09-04", "10-02", "11-06", "12-04"],
    2015: ["01-22", "03-05", "04-15", "06-03", "07-16", "09-03", "10-22", "12-03"],
    2016: ["01-21", "03-10", "04-21", "06-02", "07-21", "09-08", "10-20", "12-08"],
    2017: ["01-19", "03-09", "04-27", "06-08", "07-20", "09-07", "10-26", "12-14"],
    2018: ["01-25", "03-08", "04-26", "06-14", "07-26", "09-13", "10-25", "12-13"],
    2019: ["01-24", "03-07", "04-10", "06-06", "07-25", "09-12", "10-24", "12-12"],
    2020: ["01-23", "03-12", "04-30", "06-04", "07-16", "09-10", "10-29", "12-10"],
    2021: ["01-21", "03-11", "04-22", "06-10", "07-22", "09-09", "10-28", "12-16"],
    2022: ["02-03", "03-10", "04-14", "06-09", "07-21", "09-08", "10-27", "12-15"],
    2023: ["02-02", "03-16", "05-04", "06-15", "07-27", "09-14", "10-26", "12-14"],
    2024: ["01-25", "03-07", "04-11", "06-06", "07-18", "09-12", "10-17", "12-12"],
    2025: ["01-30", "03-06", "04-17", "06-05", "07-24", "09-11", "10-30", "12-18"],
    2026: ["02-05", "03-19", "04-30", "06-11", "07-23", "09-10", "10-29", "12-17"],
}

# BLS NFP dates that deviate from the rule (2013 government shutdown)
NFP_OVERRIDES = {(2013, 10): dt.date(2013, 10, 22), (2013, 11): dt.date(2013, 11, 8)}


def us_holidays(year):
    """Minimal US federal holidays that can collide with NFP/ISM dates."""
    def obs(d):
        if d.weekday() == 5:
            return d - dt.timedelta(days=1)
        if d.weekday() == 6:
            return d + dt.timedelta(days=1)
        return d

    def nth_weekday(month, weekday, n):
        d = dt.date(year, month, 1)
        while d.weekday() != weekday:
            d += dt.timedelta(days=1)
        return d + dt.timedelta(weeks=n - 1)

    return {
        obs(dt.date(year, 1, 1)),
        obs(dt.date(year, 7, 4)),
        obs(dt.date(year, 12, 25)),
        nth_weekday(9, 0, 1),   # Labor Day
        nth_weekday(1, 0, 3),   # MLK
        nth_weekday(2, 0, 3),   # Presidents
        nth_weekday(11, 3, 4),  # Thanksgiving
    }


def nfp_date(ref_year, ref_month):
    """Release date for the reference month's employment situation."""
    nxt = (ref_year + (ref_month == 12), ref_month % 12 + 1)
    if nxt in NFP_OVERRIDES:
        return NFP_OVERRIDES[nxt]
    d12 = dt.date(ref_year, ref_month, 12)
    # week Sun..Sat containing the 12th -> its Saturday
    sat = d12 + dt.timedelta(days=(5 - d12.weekday()) % 7)
    rel = sat + dt.timedelta(days=20)  # third Friday after that Saturday
    hol = us_holidays(rel.year)
    while rel in hol:
        rel += dt.timedelta(days=7)
    return rel


def first_business_day(year, month):
    d = dt.date(year, month, 1)
    hol = us_holidays(year)
    while d.weekday() >= 5 or d in hol:
        d += dt.timedelta(days=1)
    return d


def at(date, hhmm, tz):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime(date.year, date.month, date.day, h, m, tzinfo=tz).astimezone(UTC)


def build(y0, y1):
    ev = []  # (utc_datetime, currency, title)
    for y in range(y0, y1 + 1):
        for m in range(1, 13):
            # NFP for previous month is released in month m
            ry, rm = (y, m - 1) if m > 1 else (y - 1, 12)
            d = nfp_date(ry, rm)
            if d.year == y:
                ev.append((at(d, "08:30", NY), "USD", "Non-Farm Payrolls"))
            ev.append((at(first_business_day(y, m), "10:00", NY), "USD", "ISM Manufacturing PMI"))
        for md in FOMC.get(y, []):
            d = dt.date.fromisoformat(f"{y}-{md}")
            t = "14:00"
            if y == 2012:
                t = "12:30" if md in FOMC_2012_1230 else "14:15"
            ev.append((at(d, t, NY), "USD", "FOMC Statement"))
        for md in ECB.get(y, []):
            d = dt.date.fromisoformat(f"{y}-{md}")
            late = d >= dt.date(2022, 7, 1)
            ev.append((at(d, "14:15" if late else "13:45", CET), "EUR", "ECB Rate Decision"))
            ev.append((at(d, "14:45" if late else "14:30", CET), "EUR", "ECB Press Conference"))
    for ds, t in FOMC_EXTRA:
        d = dt.date.fromisoformat(ds)
        if y0 <= d.year <= y1:
            ev.append((at(d, t, NY), "USD", "FOMC Emergency Statement"))
    ev.sort()
    return ev


def to_server(utc_dt):
    """NY-close server time = New York local + 7h."""
    ny = utc_dt.astimezone(NY).replace(tzinfo=None)
    return ny + dt.timedelta(hours=7)


def server_epochs(y0, y1):
    """Event times as naive server-time epoch seconds (matches prep_data t_srv)."""
    out = []
    for u, _, _ in build(y0, y1):
        s = to_server(u)
        out.append(int((s - dt.datetime(1970, 1, 1)).total_seconds()))
    return sorted(set(out))


def main():
    out = sys.argv[1]
    y0 = int(sys.argv[2]) if len(sys.argv) > 2 else 2012
    y1 = int(sys.argv[3]) if len(sys.argv) > 3 else 2026
    rows = build(y0, y1)
    with open(out, "w", newline="\n") as f:
        f.write("# server_time(NY-close GMT+2/+3),currency,importance,title\n")
        for u, cur, title in rows:
            f.write(f"{to_server(u):%Y.%m.%d %H:%M},{cur},HIGH,{title}\n")
    print(f"{len(rows)} events {y0}-{y1} -> {out}")


if __name__ == "__main__":
    main()
