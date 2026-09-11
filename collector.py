#!/usr/bin/env python3
import argparse, json, os, sqlite3, sys, time
from pathlib import Path
from typing import Any
import requests

BASE_HOSTS = [
    "https://bestchange.app",
    "https://mirror1.bestchange.app",
    "https://mirror2.bestchange.app",
    "https://mirror3.bestchange.app",
    "https://mirror4.bestchange.app",
]

REF_ENDPOINTS = [
    ("langs", "/v2/{key}/langs"),
    ("groups", "/v2/{key}/groups/{lang}"),
    ("countries", "/v2/{key}/countries/{lang}"),
    ("cities", "/v2/{key}/cities/{lang}"),
    ("currencies", "/v2/{key}/currencies/{lang}"),
    ("changers", "/v2/{key}/changers/{lang}"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS raw_data (
    dataset TEXT PRIMARY KEY,
    fetched_at INTEGER NOT NULL,
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS currencies (
    id INTEGER PRIMARY KEY,
    code TEXT,
    name TEXT,
    group_id INTEGER,
    raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS changers (
    id INTEGER PRIMARY KEY,
    name TEXT,
    url TEXT,
    raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS groups_data (
    id INTEGER PRIMARY KEY,
    name TEXT,
    raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS countries (
    id INTEGER PRIMARY KEY,
    name TEXT,
    code TEXT,
    raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cities (
    id INTEGER PRIMARY KEY,
    country_id INTEGER,
    name TEXT,
    raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rates (
    from_currency_id INTEGER NOT NULL,
    to_currency_id INTEGER NOT NULL,
    city_id INTEGER,
    payload_json TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (from_currency_id, to_currency_id, city_id)
);
"""

def first(obj: dict, *keys):
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k]
    return None

def as_list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in ("data", "items", "result", "currencies", "changers", "groups", "countries", "cities", "rates", "presences"):
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        # Some API styles may return an object keyed by numeric id.
        if payload and all(isinstance(v, dict) for v in payload.values()):
            return list(payload.values())
    return []

class BCClient:
    def __init__(self, key: str, timeout: int = 20):
        self.key = key.strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Connection": "keep-alive",
            "User-Agent": "BestChangeDataCollector/1.0",
        })

    def get(self, path: str) -> tuple[Any, str]:
        last = None
        for host in BASE_HOSTS:
            url = host + path.replace("{key}", self.key)
            try:
                r = self.session.get(url, timeout=self.timeout)
                r.raise_for_status()
                return r.json(), host
            except Exception as exc:
                last = exc
        raise RuntimeError(f"BestChange API unavailable on all mirrors: {last}")

def init_db(path: Path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    return con

def save_raw(con, dataset, payload):
    now = int(time.time())
    con.execute(
        "INSERT INTO raw_data(dataset,fetched_at,json) VALUES(?,?,?) "
        "ON CONFLICT(dataset) DO UPDATE SET fetched_at=excluded.fetched_at,json=excluded.json",
        (dataset, now, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    )

def normalize_and_save(con, dataset, payload):
    rows = as_list(payload)
    if dataset == "currencies":
        con.execute("DELETE FROM currencies")
        for x in rows:
            ident = first(x, "id", "currencyId")
            if ident is None: continue
            con.execute("INSERT OR REPLACE INTO currencies VALUES (?,?,?,?,?)", (
                int(ident), first(x,"code","symbol"), first(x,"name","title"), first(x,"groupId","group_id"), json.dumps(x, ensure_ascii=False)
            ))
    elif dataset == "changers":
        con.execute("DELETE FROM changers")
        for x in rows:
            ident = first(x,"id","changerId")
            if ident is None: continue
            con.execute("INSERT OR REPLACE INTO changers VALUES (?,?,?,?)", (
                int(ident), first(x,"name","title"), first(x,"url","site","website"), json.dumps(x, ensure_ascii=False)
            ))
    elif dataset == "groups":
        con.execute("DELETE FROM groups_data")
        for x in rows:
            ident = first(x,"id","groupId")
            if ident is None: continue
            con.execute("INSERT OR REPLACE INTO groups_data VALUES (?,?,?)", (
                int(ident), first(x,"name","title"), json.dumps(x, ensure_ascii=False)
            ))
    elif dataset == "countries":
        con.execute("DELETE FROM countries")
        for x in rows:
            ident = first(x,"id","countryId")
            if ident is None: continue
            con.execute("INSERT OR REPLACE INTO countries VALUES (?,?,?,?)", (
                int(ident), first(x,"name","title"), first(x,"code","isoCode"), json.dumps(x, ensure_ascii=False)
            ))
    elif dataset == "cities":
        con.execute("DELETE FROM cities")
        for x in rows:
            ident = first(x,"id","cityId")
            if ident is None: continue
            con.execute("INSERT OR REPLACE INTO cities VALUES (?,?,?,?)", (
                int(ident), first(x,"countryId","country_id"), first(x,"name","title"), json.dumps(x, ensure_ascii=False)
            ))

def sync_reference(con, client, lang: str):
    for name, template in REF_ENDPOINTS:
        path = template.format(key="{key}", lang=lang)
        print(f"Fetching {name}...", flush=True)
        payload, host = client.get(path)
        save_raw(con, name, payload)
        normalize_and_save(con, name, payload)
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"source.{name}", host))
        con.commit()
        print(f"  OK via {host}: {len(as_list(payload))} records")
        time.sleep(0.05)
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('lang',?)", (lang,))
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_reference_sync',?)", (str(int(time.time())),))
    con.commit()

def read_pairs(path: Path) -> list[tuple[int,int,int|None]]:
    pairs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        parts = line.split("-")
        if len(parts) not in (2,3):
            raise ValueError(f"Bad pair: {line}. Expected FROM-TO or FROM-TO-CITY")
        pairs.append((int(parts[0]), int(parts[1]), int(parts[2]) if len(parts)==3 else None))
    return pairs

def fetch_rates(con, client, pairs: list[tuple[int,int,int|None]], batch_size: int = 500):
    for start in range(0, len(pairs), batch_size):
        batch = pairs[start:start+batch_size]
        tokens = [f"{a}-{b}" + (f"-{c}" if c is not None else "") for a,b,c in batch]
        path = "/v2/{key}/rates/" + "+".join(tokens)
        print(f"Fetching rates batch {start+1}-{start+len(batch)}...", flush=True)
        payload, host = client.get(path)
        now = int(time.time())
        # Keep the complete payload and also index each returned pair if identifiable.
        save_raw(con, f"rates_batch_{start}", payload)
        for token in tokens:
            a,b,c = token.split("-")
            con.execute(
                "INSERT OR REPLACE INTO rates VALUES (?,?,?,?,?)",
                (int(a), int(b), int(c) if c is not None else None, json.dumps(payload, ensure_ascii=False), now)
            )
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"source.rates_batch_{start}", host))
        con.commit()
        print(f"  OK via {host}")
        time.sleep(1.05)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="bestchange.db")
    ap.add_argument("--lang", default=os.getenv("BESTCHANGE_LANG", "ru"))
    ap.add_argument("--pairs", help="Text file with pairs: FROM-TO or FROM-TO-CITY")
    ap.add_argument("--reference-only", action="store_true")
    args = ap.parse_args()

    key = os.getenv("BESTCHANGE_API_KEY")
    if not key:
        print("BESTCHANGE_API_KEY is not set. Put it in .env or your shell environment.", file=sys.stderr)
        return 2

    con = init_db(Path(args.db))
    client = BCClient(key)
    sync_reference(con, client, args.lang)

    if args.pairs and not args.reference_only:
        pairs = read_pairs(Path(args.pairs))
        fetch_rates(con, client, pairs)
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_rates_sync',?)", (str(int(time.time())),))
        con.commit()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
