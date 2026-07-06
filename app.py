#!/usr/bin/env python3
"""
Dialled Growth Portal — run with: python3 ~/Dialled/dashboard/app.py
Then open http://localhost:5050 in your browser
"""

from flask import Flask, render_template, request, jsonify, Response
import json, os, requests
from functools import wraps
from datetime import datetime, timedelta

def load_env_file(path):
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

load_env_file(os.path.expanduser("~/.dialled-keys/dashboard.env"))

app = Flask(__name__)
DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")

SHOPIFY_STORE = "nat0ar-4y.myshopify.com"
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_TOKEN")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "dialled2026")

COGS         = 22.58
SHIPPING_OUT = 10.90
TXN_FEE_PCT  = 0.02
GROSS_MARGIN    = 0.712
BREAKEVEN_ROAS  = round(1 / GROSS_MARGIN, 2)
TARGET_CPP     = 80.0
TARGET_ROAS    = 2.50

# ---------- Password protection ----------
def check_auth(password):
    return password == DASHBOARD_PASSWORD

def authenticate():
    return Response(
        "Login required", 401,
        {"WWW-Authenticate": 'Basic realm="Dialled Portal"'}
    )

@app.before_request
def require_auth():
    auth = request.authorization
    if not auth or not check_auth(auth.password):
        return authenticate()

# ---------- Data storage ----------
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("weeks", [])
    data.setdefault("ab_tests", [])
    data.setdefault("landing_pages", [])
    return data

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ---------- Shopify ----------
def get_shopify_stats(days=7):
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        url = f"https://{SHOPIFY_STORE}/admin/api/2025-01/graphql.json"
        query = """
        {
          orders(first: 250, query: "created_at:>%s") {
            edges {
              node {
                totalPriceSet { shopMoney { amount } }
                displayFinancialStatus
              }
            }
          }
        }
        """ % since
        r = requests.post(url,
            headers={"X-Shopify-Access-Token": SHOPIFY_TOKEN, "Content-Type": "application/json"},
            json={"query": query})
        orders = r.json().get("data", {}).get("orders", {}).get("edges", [])
        paid = [o["node"] for o in orders if o["node"]["displayFinancialStatus"] in ["PAID","PARTIALLY_PAID","PARTIALLY_REFUNDED"]]
        revenue = sum(float(o["totalPriceSet"]["shopMoney"]["amount"]) for o in paid)
        count = len(paid)
        aov = revenue / count if count else 0
        return {"revenue": round(revenue, 2), "orders": count, "aov": round(aov, 2)}
    except Exception as e:
        return {"revenue": 0, "orders": 0, "aov": 0, "error": str(e)}

def get_revenue_series(days=90):
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    daily = {}
    total_revenue = 0.0
    total_orders = 0
    try:
        url = f"https://{SHOPIFY_STORE}/admin/api/2025-01/graphql.json"
        cursor = None
        for _ in range(6):
            after_clause = f', after: "{cursor}"' if cursor else ""
            query = """
            {
              orders(first: 250, query: "created_at:>%s"%s) {
                edges {
                  cursor
                  node {
                    createdAt
                    totalPriceSet { shopMoney { amount } }
                    displayFinancialStatus
                  }
                }
                pageInfo { hasNextPage }
              }
            }
            """ % (since, after_clause)
            r = requests.post(url,
                headers={"X-Shopify-Access-Token": SHOPIFY_TOKEN, "Content-Type": "application/json"},
                json={"query": query})
            result = r.json().get("data", {}).get("orders", {})
            edges = result.get("edges", [])
            if not edges:
                break
            for e in edges:
                node = e["node"]
                if node["displayFinancialStatus"] not in ["PAID", "PARTIALLY_PAID", "PARTIALLY_REFUNDED"]:
                    continue
                day = node["createdAt"][:10]
                amt = float(node["totalPriceSet"]["shopMoney"]["amount"])
                daily[day] = daily.get(day, 0) + amt
                total_revenue += amt
                total_orders += 1
            cursor = edges[-1]["cursor"]
            if not result.get("pageInfo", {}).get("hasNextPage"):
                break
        series = []
        for i in range(days, -1, -1):
            d = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
            series.append({"date": d, "revenue": round(daily.get(d, 0), 2)})
        aov = total_revenue / total_orders if total_orders else 0
        return {"series": series, "total_revenue": round(total_revenue, 2), "total_orders": total_orders, "aov": round(aov, 2)}
    except Exception as e:
        return {"series": [], "total_revenue": 0, "total_orders": 0, "aov": 0, "error": str(e)}

def get_abandoned_cart_stats(days=7):
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        url = f"https://{SHOPIFY_STORE}/admin/api/2025-01/checkouts.json"
        r = requests.get(url,
            headers={"X-Shopify-Access-Token": SHOPIFY_TOKEN},
            params={"created_at_min": since, "limit": 250})
        checkouts = r.json().get("checkouts", [])
        abandoned = len(checkouts)
        orders = get_shopify_stats(days)["orders"]
        total = abandoned + orders
        rate = (abandoned / total * 100) if total else 0
        return {"abandoned": abandoned, "orders": orders, "rate": round(rate, 1)}
    except Exception as e:
        return {"abandoned": 0, "orders": 0, "rate": 0, "error": str(e)}

# ---------- Routes ----------
def build_notifications(data, abandoned):
    notifications = []
    if abandoned.get("rate", 0) > 70:
        notifications.append({"type": "bad", "text": f"Abandoned cart rate is {abandoned['rate']}% this week"})
    elif abandoned.get("rate", 0) > 50:
        notifications.append({"type": "warn", "text": f"Abandoned cart rate is {abandoned['rate']}% this week"})
    if data["weeks"]:
        latest = data["weeks"][-1]
        if latest.get("roas", 0) < BREAKEVEN_ROAS:
            notifications.append({"type": "bad", "text": f"Latest week ROAS ({latest['roas']}) is below breakeven ({BREAKEVEN_ROAS})"})
        elif latest.get("roas", 0) < TARGET_ROAS:
            notifications.append({"type": "warn", "text": f"Latest week ROAS ({latest['roas']}) is below target ({TARGET_ROAS})"})
        if latest.get("cpp", 0) > TARGET_CPP:
            notifications.append({"type": "warn", "text": f"Latest CPP (${latest['cpp']}) is above target (${TARGET_CPP})"})
    for lp in data["landing_pages"]:
        if lp.get("views", 0) > 100 and lp.get("conversion", 0) < 1:
            notifications.append({"type": "bad", "text": f"{lp.get('page_name')} conversion is {lp['conversion']}% (low)"})
    return notifications

@app.route("/")
def index():
    data = load_data()
    shopify = get_shopify_stats(7)
    shopify_30 = get_shopify_stats(30)
    abandoned = get_abandoned_cart_stats(7)
    revenue_data = get_revenue_series(90)
    notifications = build_notifications(data, abandoned)
    return render_template("dashboard.html",
        weeks=data["weeks"],
        ab_tests=data["ab_tests"],
        landing_pages=data["landing_pages"],
        shopify=shopify,
        shopify_30=shopify_30,
        abandoned=abandoned,
        revenue_data=revenue_data,
        notifications=notifications,
        settings={
            "gross_margin": GROSS_MARGIN,
            "cogs": COGS,
            "shipping_out": SHIPPING_OUT,
            "txn_fee": TXN_FEE_PCT,
            "breakeven_roas": BREAKEVEN_ROAS,
            "target_cpp": TARGET_CPP,
            "target_roas": TARGET_ROAS,
        })

@app.route("/add_week", methods=["POST"])
def add_week():
    data = load_data()
    week = request.json
    week["gross_profit"] = round(float(week.get("meta_revenue", 0)) * GROSS_MARGIN, 2)
    week["net_profit"] = round(week["gross_profit"] - float(week.get("ad_spend", 0)), 2)
    purchases = float(week.get("purchases", 1)) or 1
    week["net_profit_per_order"] = round(week["net_profit"] / purchases, 2)
    week["roas"] = round(float(week.get("meta_revenue", 0)) / float(week.get("ad_spend", 1)), 2)
    week["cpp"] = round(float(week.get("ad_spend", 0)) / purchases, 2)
    week["aov"] = round(float(week.get("meta_revenue", 0)) / purchases, 2)
    week["id"] = datetime.now().isoformat()
    data["weeks"].append(week)
    save_data(data)
    return jsonify({"ok": True})

@app.route("/delete_week/<week_id>", methods=["DELETE"])
def delete_week(week_id):
    data = load_data()
    data["weeks"] = [w for w in data["weeks"] if w.get("id") != week_id]
    save_data(data)
    return jsonify({"ok": True})

@app.route("/add_ab_test", methods=["POST"])
def add_ab_test():
    data = load_data()
    test = request.json
    test["id"] = datetime.now().isoformat()
    data["ab_tests"].append(test)
    save_data(data)
    return jsonify({"ok": True})

@app.route("/delete_ab_test/<test_id>", methods=["DELETE"])
def delete_ab_test(test_id):
    data = load_data()
    data["ab_tests"] = [t for t in data["ab_tests"] if t.get("id") != test_id]
    save_data(data)
    return jsonify({"ok": True})

@app.route("/add_landing_page", methods=["POST"])
def add_landing_page():
    data = load_data()
    lp = request.json
    views = float(lp.get("views", 0)) or 0
    atc = float(lp.get("atc", 0)) or 0
    orders = float(lp.get("orders", 0)) or 0
    revenue = float(lp.get("revenue", 0)) or 0
    lp["atc_rate"] = round(atc / views * 100, 1) if views else 0
    lp["conversion"] = round(orders / views * 100, 1) if views else 0
    lp["aov"] = round(revenue / orders, 2) if orders else 0
    lp["id"] = datetime.now().isoformat()
    data["landing_pages"].append(lp)
    save_data(data)
    return jsonify({"ok": True})

@app.route("/delete_landing_page/<lp_id>", methods=["DELETE"])
def delete_landing_page(lp_id):
    data = load_data()
    data["landing_pages"] = [l for l in data["landing_pages"] if l.get("id") != lp_id]
    save_data(data)
    return jsonify({"ok": True})

@app.route("/shopify_stats")
def shopify_stats():
    days = int(request.args.get("days", 7))
    return jsonify(get_shopify_stats(days))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    if not os.environ.get("PORT"):
        import webbrowser, threading
        def open_browser():
            import time; time.sleep(1)
            webbrowser.open(f"http://localhost:{port}")
        threading.Thread(target=open_browser).start()
        print(f"\n🚀 Dialled Portal running at http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
