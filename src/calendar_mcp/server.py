import datetime as dt

from Foundation import NSDate

ns = NSDate.dateWithTimeIntervalSince1970_(dt.datetime(2026, 10, 15, 14, 0).timestamp())
back = dt.datetime.fromtimestamp(ns.timeIntervalSince1970())