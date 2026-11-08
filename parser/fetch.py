"""Скачивает график ДТЕК и печатает нормализованный JSON в stdout (для GitHub Actions)."""
import argparse
import json
import sys

import dtek

ap = argparse.ArgumentParser()
ap.add_argument("--headed", action="store_true")
a = ap.parse_args()

data = dtek.fetch(headless=not a.headed)
if not data["groups"]:
    sys.exit("пустой график")
data["source"] = "dtek-oem.com.ua"
json.dump(data, sys.stdout, ensure_ascii=False, separators=(",", ":"))
