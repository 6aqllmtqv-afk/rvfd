#!/usr/bin/env python3
import os, json, re, time
from pathlib import Path
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv
import requests

load_dotenv()
BASE_HOSTS = [
    "https://bestchange.app",
    "https://mirror1.bestchange.app",
    "https://mirror2.bestchange.app",
    "https://mirror3.bestchange.app",
    "https://mirror4.bestchange.app",
]
KEY = os.getenv("BESTCHANGE_API_KEY", "").strip()
LANG = os.getenv("BESTCHANGE_LANG", "ru")
TIMEOUT = int(os.getenv("BESTCHANGE_TIMEOUT", "30"))

app = Flask(__name__)
session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
    "Connection": "keep-alive",
    "User-Agent": "BestChangeDataCollector-Web/1.0",
})

cache = {}
CACHE_TTL = int(os.getenv("BESTCHANGE_CACHE_TTL", "1"))

def api_get(path):
    if not KEY:
        raise RuntimeError("BESTCHANGE_API_KEY is not set")
    last = None
    for host in BASE_HOSTS:
        try:
            r = session.get(host + path, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json(), host
        except Exception as exc:
            last = exc
    raise RuntimeError(str(last))

def listify(payload, preferred=()):
    """Turn API list/map wrappers into a list of records.

    BestChange responses can be represented as JSON arrays or as objects keyed by
    IDs. Some deployments also wrap the collection in data/items/result. Keep
    this parser deliberately tolerant so the UI does not silently show an empty
    selector when the API shape changes slightly.
    """
    record_keys = {
        "id", "currencyId", "changerId", "countryId", "cityId",
        "groupId", "code", "name", "title", "url", "site", "website"
    }

    def is_record(x):
        if not isinstance(x, dict):
            return False
        return any(k in x for k in record_keys)

    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if not isinstance(payload, dict):
        return []

    # Preferred collection wrappers first.
    for k in tuple(preferred) + ("data", "items", "result", "currencies", "changers", "presences", "rates"):
        if k in payload:
            v = payload[k]
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                direct = [x for x in v.values() if is_record(x)]
                if direct:
                    return direct
                nested = listify(v)
                if nested:
                    return nested

    # Flat object keyed by IDs.
    vals = list(payload.values())
    direct = [x for x in vals if is_record(x)]
    if direct and len(direct) >= max(1, len(vals) // 2):
        return direct

    # Last-resort recursive walk for a collection nested one or two levels deep.
    out = []
    for v in vals:
        if isinstance(v, dict):
            if is_record(v):
                out.append(v)
            else:
                nested = listify(v)
                if nested:
                    out.extend(nested)
    return out

def pick(o, *keys, default=None):
    for k in keys:
        if isinstance(o, dict) and k in o and o[k] is not None:
            return o[k]
    return default

def cached(key, loader):
    now = time.time()
    item = cache.get(key)
    if item and now - item[0] < CACHE_TTL:
        return item[1]
    value = loader()
    cache[key] = (now, value)
    return value

@app.get("/")
def index():
    return render_template("index.html")

@app.get("/api/currencies")
def currencies():
    def load():
        p, host = api_get(f"/v2/{KEY}/currencies/{LANG}")
        rows = listify(p, ("currencies",))
        out = []
        for x in rows:
            cid = pick(x, "id", "currencyId")
            if cid is None: continue
            name = pick(x, "name", "title", "code", default=str(cid))
            if isinstance(name, dict):
                name = name.get(LANG) or name.get("ru") or name.get("en") or next(iter(name.values()), str(cid))
            out.append({
                "id": int(cid),
                "name": str(name),
                "code": str(pick(x, "code", "symbol", default="") or ""),
                "groupId": pick(x, "groupId", "group_id"),
                "raw": x,
            })
        return {"host": host, "items": out}
    try: return jsonify(cached("currencies", load))
    except Exception as e: return jsonify({"error": str(e)}), 502

@app.get("/api/changers")
def changers():
    def load():
        p, host = api_get(f"/v2/{KEY}/changers/{LANG}")
        rows = listify(p, ("changers",))
        out=[]
        for x in rows:
            cid=pick(x,"id","changerId")
            if cid is None: continue
            name = pick(x, "name", "title", default=str(cid))
            if isinstance(name, dict):
                name = name.get(LANG) or name.get("ru") or name.get("en") or next(iter(name.values()), str(cid))
            out.append({"id":int(cid),"name":str(name),"url":str(pick(x,"url","site","website",default="") or ""),"raw":x})
        return {"host": host, "items": out}
    try: return jsonify(cached("changers", load))
    except Exception as e: return jsonify({"error": str(e)}), 502

def normalize_rate(row):
    # Accept several aliases so UI survives minor API field-name differences.
    return {
        "changerId": pick(row,"changerId","changer_id","id"),
        "rate": pick(row,"rate","price","exchangeRate","exchange_rate"),
        "reserve": pick(row,"reserve","amount","reserveAmount","reserve_amount"),
        "min": pick(row,"min","minAmount","min_amount","minSum","min_sum"),
        "max": pick(row,"max","maxAmount","max_amount","maxSum","max_sum"),
        "fromFee": pick(row,"fromFee","from_fee"),
        "toFee": pick(row,"toFee","to_fee"),
        "fee": pick(row,"fee"),
        "updatedAt": pick(row,"updatedAt","updated_at","timestamp"),
        "raw": row,
    }

@app.get("/api/rates")
def rates():
    a=request.args.get("from", type=int)
    b=request.args.get("to", type=int)
    c=request.args.get("city", type=int)
    if not a or not b:
        return jsonify({"error":"from and to are required"}),400
    token=f"{a}-{b}" + (f"-{c}" if c else "")
    def load():
        p,host=api_get(f"/v2/{KEY}/rates/{token}")
        rows=listify(p,("rates",))
        normalized=[normalize_rate(x) for x in rows if isinstance(x,dict)]
        return {"from":a,"to":b,"city":c,"host":host,"count":len(normalized),"rates":normalized,"raw":p}
    try: return jsonify(cached("rates:"+token,load))
    except Exception as e: return jsonify({"error": str(e)}),502

@app.get("/api/presences")
def presences():
    a=request.args.get("from", type=int); b=request.args.get("to", type=int); c=request.args.get("city", type=int)
    if not a or not b: return jsonify({"error":"from and to are required"}),400
    token=f"{a}-{b}" + (f"-{c}" if c else "")
    try:
        p,host=api_get(f"/v2/{KEY}/presences/{token}")
        return jsonify({"from":a,"to":b,"city":c,"host":host,"items":listify(p,("presences",)),"raw":p})
    except Exception as e: return jsonify({"error":str(e)}),502

@app.get("/api/health")
def health():
    return jsonify({"ok":bool(KEY),"lang":LANG,"cacheTtl":CACHE_TTL})

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8080")),debug=False)
