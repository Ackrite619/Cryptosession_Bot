"""
Crypto Session Bot -- Alpaca Edition
Receives ForexBot-style session ICC signals from TradingView
and executes on Alpaca paper/live for crypto pairs (BTC, ETH, SOL, LTC)
"""

import os
import json
import logging
import threading
import time
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    TrailingStopOrderRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

load_dotenv()

# 
# CONFIGURATION
# 
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET", "cryptosession123")
PAPER             = os.getenv("PAPER", "true").lower() == "true"
MAX_HOLD_HOURS    = float(os.getenv("MAX_HOLD_HOURS", "24"))
DAILY_TRADE_LIMIT = int(os.getenv("DAILY_TRADE_LIMIT", "2"))
MAX_SL_DISTANCE_PCT = float(os.getenv("MAX_SL_DISTANCE_PCT", "10"))

PAIRS = ["BTCUSD", "ETHUSD", "SOLUSD", "LTCUSD"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
trade_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER)

# 
# STATE
# 
def make_state():
    return {
        "in_trade":    False,
        "direction":   None,
        "entry_price": None,
        "sl":          None,
        "tp1":         None,
        "tp2":         None,
        "qty":         None,
        "symbol":      None,
        "opened_at":   None,
        "signal_type": None,
        "session":     None,
        "tp1_hit":     False,
        "daily_count": 0,
    }

states = {pair: make_state() for pair in PAIRS}
trade_history = []
daily_reset_date = datetime.now(timezone.utc).date()

def reset_daily_if_needed():
    global daily_reset_date
    today = datetime.now(timezone.utc).date()
    if today != daily_reset_date:
        daily_reset_date = today
        for s in states.values():
            s["daily_count"] = 0
        log.info("Daily trade counts reset")

def normalize_instrument(raw):
    clean = raw.upper().replace("/", "").replace("-", "").replace("_", "")
    for pair in PAIRS:
        if clean == pair or clean == pair.replace("USD","") + "USD":
            return pair
    return clean

def reset_state(instrument):
    s = states[instrument]
    s.update({
        "in_trade":    False,
        "direction":   None,
        "entry_price": None,
        "sl":          None,
        "tp1":         None,
        "tp2":         None,
        "qty":         None,
        "symbol":      None,
        "opened_at":   None,
        "signal_type": None,
        "session":     None,
        "tp1_hit":     False,
    })

def record_trade_close(instrument, reason):
    s = states[instrument]
    if s["in_trade"]:
        trade_history.append({
            "instrument":  instrument,
            "direction":   s["direction"],
            "entry_price": s["entry_price"],
            "sl":          s["sl"],
            "tp1":         s["tp1"],
            "tp2":         s["tp2"],
            "qty":         s["qty"],
            "signal_type": s["signal_type"],
            "session":     s["session"],
            "opened_at":   s["opened_at"],
            "closed_at":   datetime.now(timezone.utc).isoformat(),
            "close_reason": reason,
            "tp1_hit":     s["tp1_hit"],
        })

# 
# ALPACA HELPERS
# 
def get_account():
    try:
        return trade_client.get_account()
    except Exception as e:
        log.error(f"get_account error: {e}")
        return None

def get_open_position(symbol):
    try:
        clean = symbol.replace("/", "")
        return trade_client.get_open_position(clean)
    except Exception as e:
        status = getattr(e, "status_code", None)
        if status == 404:
            return None
        log.error(f"get_open_position error for {symbol}: {e}")
        return None

def real_position_exists(symbol):
    try:
        clean = symbol.replace("/", "")
        trade_client.get_open_position(clean)
        return True
    except Exception as e:
        status = getattr(e, "status_code", None)
        if status == 404:
            return False
        log.error(f"real_position_exists check failed for {symbol}: {e}")
        return True  # fail safe

def place_order(symbol, qty, side):
    try:
        clean = symbol.replace("/", "")
        req = MarketOrderRequest(
            symbol=clean,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.GTC
        )
        result = trade_client.submit_order(req)
        log.info(f"Order placed: {side} {qty} {clean} | id={result.id}")
        return str(result.id)
    except Exception as e:
        log.error(f"place_order error: {e}")
        return None

def cancel_all_orders():
    try:
        trade_client.cancel_orders()
        log.info("All orders cancelled")
    except Exception as e:
        log.error(f"cancel_all_orders error: {e}")

def close_position(symbol):
    try:
        clean = symbol.replace("/", "")
        trade_client.close_position(clean)
        log.info(f"Position closed: {symbol}")
        return True
    except Exception as e:
        log.error(f"close_position error: {e}")
        return False

def force_close_position(instrument, symbol, reason):
    cancel_all_orders()
    success = close_position(symbol)
    if not success:
        log.error(f"force_close ({instrument}, {reason}): first attempt failed -- retrying in 10s")
        time.sleep(10)
        cancel_all_orders()
        success = close_position(symbol)
    if success:
        log.warning(f"force_close ({instrument}, {reason}): closed successfully")
    else:
        log.error(f"force_close ({instrument}, {reason}): BOTH attempts failed -- resetting state anyway")
    record_trade_close(instrument, reason)
    reset_state(instrument)

# 
# VALIDATE LEVELS
# 
def validate_levels(direction, entry, sl, tp1, tp2):
    if None in (entry, sl, tp1, tp2):
        return False, "missing_levels"
    sl_pct = abs(entry - sl) / entry * 100
    if sl_pct > MAX_SL_DISTANCE_PCT:
        return False, f"sl_too_wide_{sl_pct:.1f}pct_max_{MAX_SL_DISTANCE_PCT}"
    if sl_pct < 0.05:
        return False, f"sl_too_tight_{sl_pct:.2f}pct"
    if direction == "LONG":
        if not (sl < entry < tp1 < tp2):
            return False, "invalid_long_levels"
    else:
        if not (tp2 < tp1 < entry < sl):
            return False, "invalid_short_levels"
    return True, "ok"

# 
# HANDLE ENTER
# 
def handle_enter(signal):
    reset_daily_if_needed()

    instrument = normalize_instrument(signal.get("instrument", "BTCUSD"))
    direction  = signal.get("action", "").upper()
    sig_type   = signal.get("type", "SESSION")
    session    = signal.get("session", "UNKNOWN")

    if instrument not in states:
        return {"status": "error", "reason": "unknown_instrument", "instrument": instrument}

    s = states[instrument]

    if s["in_trade"]:
        log.warning(f"Already in trade for {instrument} -- ignoring")
        return {"status": "ignored", "reason": "already_in_trade", "instrument": instrument}

    if DAILY_TRADE_LIMIT > 0 and not sig_type.startswith("TEST"):
        if s["daily_count"] >= DAILY_TRADE_LIMIT:
            log.warning(f"Daily limit reached for {instrument}: {s['daily_count']}/{DAILY_TRADE_LIMIT}")
            return {"status": "ignored", "reason": "daily_limit_reached", "instrument": instrument}

    entry = float(signal.get("entry", 0))
    sl    = float(signal.get("sl",    0))
    tp1   = float(signal.get("tp1",   0))
    tp2   = float(signal.get("tp2",   0))
    risk_pct = float(signal.get("risk_pct", 1.0))

    ok, reason = validate_levels(direction, entry, sl, tp1, tp2)
    if not ok:
        log.warning(f"Invalid levels for {instrument}: {reason}")
        return {"status": "error", "reason": reason, "instrument": instrument}

    # Position sizing from account balance
    account = get_account()
    if not account:
        return {"status": "error", "reason": "account_unavailable"}
    balance = float(account.cash)
    sl_dist = abs(entry - sl)
    dollar_risk = balance * (risk_pct / 100.0)
    qty = max(0.001, round(dollar_risk / sl_dist, 3))

    # Safety cap: position notional value must not exceed available balance
    # (crypto qty directly multiplies full asset price, unlike forex pip-based units)
    max_qty_by_balance = (balance * 0.95) / entry  # use 95% of balance as ceiling, leaves buffer
    if qty > max_qty_by_balance:
        log.warning(f"Sized qty {qty} exceeds balance cap, reducing to {max_qty_by_balance:.4f}")
        qty = max(0.001, round(max_qty_by_balance, 4))

    side = "buy" if direction == "LONG" else "sell"
    log.info(f"SESSION ENTRY: {instrument} {direction} {session} entry={entry} sl={sl} tp1={tp1} tp2={tp2} qty={qty}")

    order_id = place_order(instrument, qty, side)
    if not order_id:
        return {"status": "error", "reason": "order_failed", "instrument": instrument}

    if not sig_type.startswith("TEST"):
        s["daily_count"] += 1

    s.update({
        "in_trade":    True,
        "direction":   direction,
        "entry_price": entry,
        "sl":          sl,
        "tp1":         tp1,
        "tp2":         tp2,
        "qty":         qty,
        "symbol":      instrument,
        "opened_at":   datetime.now(timezone.utc).isoformat(),
        "signal_type": sig_type,
        "session":     session,
        "tp1_hit":     False,
    })

    log.info(f"SESSION TRADE OPEN: {instrument} {direction} {session} qty={qty} daily={s['daily_count']}")
    return {
        "status":      "success",
        "instrument":  instrument,
        "direction":   direction,
        "session":     session,
        "qty":         qty,
        "sl":          sl,
        "tp1":         tp1,
        "tp2":         tp2,
        "daily_count": s["daily_count"],
    }

# 
# HANDLE TP1 HIT
# 
def handle_tp1_hit(signal):
    instrument = normalize_instrument(signal.get("instrument", "BTCUSD"))
    s = states.get(instrument)
    if not s or not s["in_trade"] or s["tp1_hit"]:
        return {"status": "ignored", "instrument": instrument}

    qty        = s["qty"]
    entry      = s["entry_price"]
    direction  = s["direction"]
    tp2        = s["tp2"]

    qty_close  = max(0.001, round(qty / 2, 3))
    qty_remain = max(0.001, round(qty - qty_close, 3))

    cancel_all_orders()
    close_side = "sell" if direction == "LONG" else "buy"
    order_id = place_order(instrument, qty_close, close_side)

    if not order_id:
        return {"status": "error", "reason": "tp1_partial_failed", "instrument": instrument}

    s["tp1_hit"] = True
    s["qty"]     = qty_remain
    s["sl"]      = entry  # move to breakeven

    log.info(f"TP1 HIT {instrument}: closed {qty_close}, {qty_remain} remain to TP2={tp2}, SL moved to BE={entry}")
    return {
        "status":        "tp1_hit",
        "instrument":    instrument,
        "qty_closed":    qty_close,
        "qty_remaining": qty_remain,
        "sl_moved_to":   entry,
        "tp2":           tp2,
    }

# 
# HANDLE EXIT
# 
def handle_exit(signal):
    instrument = normalize_instrument(signal.get("instrument", "BTCUSD"))
    s = states.get(instrument)
    if not s or not s["in_trade"]:
        return {"status": "ignored", "reason": "not_in_trade", "instrument": instrument}

    cancel_all_orders()
    close_position(instrument)
    record_trade_close(instrument, "EXIT_SIGNAL")
    reset_state(instrument)
    log.info(f"EXIT {instrument}")
    return {"status": "closed", "instrument": instrument}

# 
# HOLD TIME MONITOR
# 
def hold_time_monitor():
    log.info(f"Hold monitor started -- max {MAX_HOLD_HOURS}h")
    while True:
        time.sleep(300)
        try:
            reset_daily_if_needed()
            now = datetime.now(timezone.utc)
            for instrument, s in states.items():
                if not s["in_trade"] or not s["opened_at"]:
                    continue
                if not real_position_exists(s["symbol"]):
                    log.warning(f"STATE DESYNC: {instrument} in_trade=True but no real position -- self-correcting")
                    record_trade_close(instrument, "STATE_DESYNC_AUTO_CORRECTED")
                    reset_state(instrument)
                    continue
                opened = datetime.fromisoformat(s["opened_at"])
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                hold_hours = (now - opened).total_seconds() / 3600
                if MAX_HOLD_HOURS > 0 and hold_hours >= MAX_HOLD_HOURS:
                    log.warning(f"MAX HOLD: {instrument} {hold_hours:.1f}h -- auto-closing")
                    force_close_position(instrument, s["symbol"], "MAX_HOLD_TIME")
        except Exception as e:
            log.error(f"Hold monitor error: {e}")

# 
# WEBHOOK
# 
@app.route("/webhook", methods=["POST"])
def webhook():
    secret = request.args.get("secret")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid_json"}), 400

    log.info(f"Signal: {json.dumps(data)}")
    action = data.get("action", "").upper()

    if action in ("LONG", "SHORT"):
        result = handle_enter(data)
    elif action == "TP1":
        result = handle_tp1_hit(data)
    elif action == "EXIT":
        result = handle_exit(data)
    else:
        result = {"status": "ignored", "reason": "unknown_action", "action": action}

    log.info(f"Result: {json.dumps(result)}")
    return jsonify(result)

@app.route("/status", methods=["GET"])
def status():
    reset_daily_if_needed()
    account = get_account()
    return jsonify({
        "cash":          str(account.cash) if account else None,
        "equity":        str(account.equity) if account else None,
        "paper":         PAPER,
        "max_hold_hours": MAX_HOLD_HOURS,
        "daily_limit":   DAILY_TRADE_LIMIT,
        "pairs":         states,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    })

@app.route("/trades", methods=["GET"])
def trades():
    return jsonify({"trades": trade_history, "count": len(trade_history)})

@app.route("/close_all", methods=["POST"])
def close_all():
    secret = request.args.get("secret")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    cancel_all_orders()
    for instrument, s in states.items():
        if s["in_trade"] and s["symbol"]:
            force_close_position(instrument, s["symbol"], "MANUAL_CLOSE_ALL")
    log.warning("CLOSE ALL triggered")
    return jsonify({"status": "all_closed"})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})

@app.route("/", methods=["GET"])
def index():
    return jsonify({"bot": "Crypto Session Bot v1", "pairs": PAIRS, "paper": PAPER})

# 
# STARTUP
# 
if __name__ == "__main__":
    log.info("Crypto Session Bot v1 -- Alpaca Edition")
    log.info(f"Paper: {PAPER} | Pairs: {', '.join(PAIRS)}")
    log.info(f"Max hold: {MAX_HOLD_HOURS}h | Daily limit: {DAILY_TRADE_LIMIT}")
    monitor_thread = threading.Thread(target=hold_time_monitor, daemon=True)
    monitor_thread.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)
