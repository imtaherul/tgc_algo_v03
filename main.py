"""
FastAPI Trading Server — Binance USDT-M Futures
Run: uvicorn main:app --reload
"""
import time
import logging
import asyncio
import json
import threading
from datetime import datetime
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from binance.um_futures import UMFutures
from binance.error import ClientError
from keys import api, secret

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Binance Futures Trader")
templates = Jinja2Templates(directory="templates")

# ── In-memory log store ────────────────────────────────────────
_terminal_logs: list[dict] = []
_log_subscribers: list[asyncio.Queue] = []
_event_loop: asyncio.AbstractEventLoop = None

def push_log(level: str, msg: str):
    entry = {"ts": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg}
    _terminal_logs.append(entry)
    if len(_terminal_logs) > 200:
        _terminal_logs.pop(0)
    if _event_loop and not _event_loop.is_closed():
        for q in list(_log_subscribers):
            try:
                _event_loop.call_soon_threadsafe(q.put_nowait, entry)
            except Exception as e:
                logger.warning(f"push_log broadcast failed: {e}")

# ── Shared config ──────────────────────────────────────────────
USE_TESTNET   = False
SYMBOL        = "BTCUSDT"
LEVERAGE      = 10
AMOUNT        = 1650.0
TP_OFFSET     = 1000.0
SL_OFFSET     = 300.0
WORKING_TYPE  = "MARK_PRICE"
POLL_INTERVAL = 5
MONITOR_INTERVAL = 10   # seconds between position checks

def get_client() -> UMFutures:
    if USE_TESTNET:
        return UMFutures(key=api, secret=secret,
                         base_url="https://testnet.binancefuture.com")
    return UMFutures(key=api, secret=secret)

def fmt_price(p: float) -> str: return f"{p:.2f}"
def fmt_qty(q: float)   -> str: return f"{q:.3f}"
def exit_side(side: str) -> str: return "SELL" if side == "BUY" else "BUY"

def tpl_ctx(request: Request, extra: dict = {}) -> dict:
    return {
        "request":      request,
        "cfg_amount":   AMOUNT,
        "cfg_leverage": LEVERAGE,
        "cfg_symbol":   SYMBOL,
        "cfg_tp":       TP_OFFSET,
        "cfg_sl":       SL_OFFSET,
        "initial_logs": _terminal_logs[-50:],
        **extra,
    }

# ── Position monitor — auto-cancels orphan algo orders ─────────
def _cancel_algo(client: UMFutures, algo_id: int, reason: str):
    try:
        client.sign_request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
        push_log("WARN", f"🗑  Auto-cancelled algo #{algo_id}  ({reason})")
    except Exception as e:
        push_log("ERROR", f"✗  Failed to auto-cancel algo #{algo_id}: {e}")

def position_monitor():
    """
    Background thread: every MONITOR_INTERVAL seconds —
      1. Fetch open positions for SYMBOL
      2. Fetch open algo orders for SYMBOL
      3. If position size == 0 but algo orders exist → cancel all of them
         (position was closed by TP, SL, or manual — orphan algo orders remain)
    """
    push_log("INFO", f"🔍 Position monitor started (every {MONITOR_INTERVAL}s)")
    client = get_client()

    # Track previous position size to detect transitions
    prev_pos_size: float = None

    while True:
        try:
            time.sleep(MONITOR_INTERVAL)

            # ── Get position ─────────────────────────────────────
            positions = client.sign_request("GET", "/fapi/v2/positionRisk", {"symbol": SYMBOL})
            pos_size = 0.0
            for p in (positions if isinstance(positions, list) else []):
                if p.get("symbol") == SYMBOL:
                    pos_size = abs(float(p.get("positionAmt", 0)))
                    break

            # ── Get open algo orders ──────────────────────────────
            algo_resp = client.sign_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": SYMBOL})
            if isinstance(algo_resp, dict):
                algo_orders = (algo_resp.get("orders")
                               or algo_resp.get("algoOrders")
                               or algo_resp.get("data")
                               or [])
            elif isinstance(algo_resp, list):
                algo_orders = algo_resp
            else:
                algo_orders = []

            # ── Detect position closed with orphan algo orders ────
            if pos_size == 0.0 and algo_orders:
                push_log("WARN",  "⚠  Position is CLOSED but algo orders still open — cleaning up...")
                for o in algo_orders:
                    algo_id   = o.get("algoId")
                    algo_type = o.get("orderType", o.get("type", "?"))
                    trigger   = o.get("triggerPrice", "?")
                    push_log("INFO", f"   Cancelling orphan {algo_type} algo #{algo_id}  trigger: ${trigger}")
                    _cancel_algo(client, algo_id, "position closed")
                push_log("SUCCESS", "✓  All orphan algo orders cancelled")

            # ── Log transition: position opened or closed ─────────
            if prev_pos_size is not None:
                if prev_pos_size == 0.0 and pos_size > 0.0:
                    push_log("INFO", f"📈 Position OPENED — size: {pos_size} BTC")
                elif prev_pos_size > 0.0 and pos_size == 0.0:
                    push_log("SUCCESS", f"✅ Position CLOSED — was {prev_pos_size} BTC")

            prev_pos_size = pos_size

        except Exception as e:
            logger.warning(f"Position monitor error: {e}")


# ── Order execution (runs in background thread) ────────────────
def run_order(side: str, limit_price: float, amount: float,
              tp_offset: float, sl_offset: float):
    client = get_client()
    quantity    = amount * LEVERAGE / limit_price
    take_profit = limit_price + tp_offset if side == "BUY" else limit_price - tp_offset
    stop_loss   = limit_price - sl_offset if side == "BUY" else limit_price + sl_offset

    push_log("INFO",  "─" * 44)
    push_log("INFO",  f"▶ Starting {side} order sequence")
    push_log("INFO",  f"  Symbol   : {SYMBOL}")
    push_log("INFO",  f"  Margin   : ${amount} USDT  |  Leverage: {LEVERAGE}x")
    push_log("INFO",  f"  Entry    : ${fmt_price(limit_price)}")
    push_log("INFO",  f"  Quantity : {fmt_qty(quantity)} BTC")
    push_log("INFO",  f"  TP target: ${fmt_price(take_profit)}  (+{tp_offset})")
    push_log("INFO",  f"  SL target: ${fmt_price(stop_loss)}  (-{sl_offset})")
    push_log("INFO",  "─" * 44)

    try:
        # Step 1: Set leverage
        push_log("INFO", "⚙  [1/4] Setting leverage...")
        resp = client.change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
        push_log("SUCCESS", f"✓  Leverage set to {resp['leverage']}x for {resp['symbol']}")

        # Step 2: Place limit entry
        push_log("INFO", f"📤 [2/4] Placing LIMIT {side} @ ${fmt_price(limit_price)} | qty: {fmt_qty(quantity)}")
        entry = client.new_order(
            symbol=SYMBOL, side=side, type="LIMIT",
            timeInForce="GTC",
            quantity=fmt_qty(quantity),
            price=fmt_price(limit_price),
        )
        entry_id = entry["orderId"]
        push_log("SUCCESS", f"✓  Entry order placed — orderId: {entry_id}")

        # Step 3: Wait for fill
        push_log("INFO", f"⏳ [3/4] Waiting for order {entry_id} to fill...")
        attempt = 0
        while True:
            attempt += 1
            order  = client.query_order(symbol=SYMBOL, orderId=entry_id)
            status = order["status"]
            filled = float(order.get("executedQty", 0))
            push_log("INFO", f"   Poll #{attempt} → status: {status}  filled: {filled}/{fmt_qty(quantity)} BTC")
            if status == "FILLED":
                push_log("SUCCESS", f"✓  Order FILLED! avg price: ${order.get('avgPrice','?')}")
                break
            if status in ("CANCELED", "REJECTED", "EXPIRED"):
                push_log("ERROR", f"✗  Order ended with status: {status} — aborting")
                return
            if status == "PARTIALLY_FILLED":
                push_log("WARN", f"   Partially filled ({filled} BTC) — still waiting...")
            push_log("INFO", f"   Next poll in {POLL_INTERVAL}s...")
            time.sleep(POLL_INTERVAL)

        # Step 4: Place TP + SL via Algo Order API
        push_log("INFO", "📤 [4/4] Placing Take Profit & Stop Loss via Algo Order API...")
        tp_exec = take_profit - 5  if side == "BUY" else take_profit + 5
        sl_exec = stop_loss  - 10  if side == "BUY" else stop_loss  + 10

        push_log("INFO", f"   → TP: TAKE_PROFIT {exit_side(side)} | trigger: ${fmt_price(take_profit)} | exec: ${fmt_price(tp_exec)}")
        tp = client.sign_request("POST", "/fapi/v1/algoOrder", {
            "symbol":       SYMBOL,
            "side":         exit_side(side),
            "algoType":     "CONDITIONAL",
            "type":         "TAKE_PROFIT",
            "quantity":     fmt_qty(quantity),
            "price":        fmt_price(tp_exec),
            "triggerPrice": fmt_price(take_profit),
            "timeInForce":  "GTC",
            "workingType":  WORKING_TYPE,
            "reduceOnly":   "true",
        })
        tp_id = tp.get("algoId", "?")
        push_log("SUCCESS", f"✓  Take Profit placed | trigger: ${fmt_price(take_profit)} — algoId: {tp_id}")

        push_log("INFO", f"   → SL: STOP {exit_side(side)} | trigger: ${fmt_price(stop_loss)} | exec: ${fmt_price(sl_exec)}")
        sl = client.sign_request("POST", "/fapi/v1/algoOrder", {
            "symbol":       SYMBOL,
            "side":         exit_side(side),
            "algoType":     "CONDITIONAL",
            "type":         "STOP",
            "quantity":     fmt_qty(quantity),
            "price":        fmt_price(sl_exec),
            "triggerPrice": fmt_price(stop_loss),
            "timeInForce":  "GTC",
            "workingType":  WORKING_TYPE,
            "reduceOnly":   "true",
        })
        sl_id = sl.get("algoId", "?")
        push_log("SUCCESS", f"✓  Stop Loss placed   | trigger: ${fmt_price(stop_loss)} — algoId: {sl_id}")

        push_log("INFO",    "─" * 44)
        push_log("SUCCESS", "🏁 ALL ORDERS COMPLETE")
        push_log("SUCCESS", f"   Entry: #{entry_id}  TP algo: #{tp_id}  SL algo: #{sl_id}")
        push_log("INFO",    "─" * 44)

    except ClientError as e:
        push_log("ERROR", f"✗  Binance error: {e.error_code} — {e.error_message}")
        push_log("ERROR", "─" * 44)
    except Exception as e:
        push_log("ERROR", f"✗  Unexpected error: {str(e)}")
        push_log("ERROR", "─" * 44)


# ── Routes ─────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global _event_loop
    _event_loop = asyncio.get_running_loop()
    # Start position monitor in background daemon thread
    t = threading.Thread(target=position_monitor, daemon=True)
    t.start()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    push_log("INFO", "Dashboard loaded")
    return templates.TemplateResponse("index.html", tpl_ctx(request))

@app.post("/place-order")
async def place_order(
    request:     Request,
    side:        str   = Form(...),
    limit_price: float = Form(...),
    margin:      float = Form(default=0),
    tp_offset:   float = Form(default=0),
    sl_offset:   float = Form(default=0),
):
    amount = margin    if margin    > 0 else AMOUNT
    tp     = tp_offset if tp_offset > 0 else TP_OFFSET
    sl     = sl_offset if sl_offset > 0 else SL_OFFSET
    thread = threading.Thread(
        target=run_order,
        args=(side, limit_price, amount, tp, sl),
        daemon=True,
    )
    thread.start()
    return JSONResponse({"status": "started", "side": side, "price": limit_price})

@app.get("/open-orders")
async def open_orders():
    client = get_client()
    try:
        regular = client.sign_request("GET", "/fapi/v1/openOrders", {"symbol": SYMBOL}) or []
        if not isinstance(regular, list):
            regular = []
    except Exception as e:
        push_log("ERROR", f"Failed to fetch regular orders: {e}")
        regular = []
    algo = []
    try:
        algo_resp = client.sign_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": SYMBOL})
        if isinstance(algo_resp, dict):
            algo = (algo_resp.get("orders")
                    or algo_resp.get("algoOrders")
                    or algo_resp.get("data")
                    or [])
        elif isinstance(algo_resp, list):
            algo = algo_resp
    except Exception as e:
        push_log("ERROR", f"Failed to fetch algo orders: {e}")
    return JSONResponse({"regular": regular, "algo": algo})

@app.post("/cancel-order")
async def cancel_order(order_id: str = Form(...), order_type: str = Form(default="regular")):
    client = get_client()
    try:
        if order_type == "algo":
            client.sign_request("DELETE", "/fapi/v1/algoOrder", {"algoId": int(order_id)})
            push_log("WARN", f"🗑  Algo order #{order_id} cancelled")
        else:
            client.cancel_order(symbol=SYMBOL, orderId=int(order_id))
            push_log("WARN", f"🗑  Order #{order_id} cancelled")
        return JSONResponse({"status": "cancelled", "orderId": order_id})
    except Exception as e:
        push_log("ERROR", f"✗  Cancel failed for #{order_id}: {e}")
        return JSONResponse({"status": "error", "msg": str(e)}, status_code=400)

@app.get("/logs/stream")
async def log_stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _log_subscribers.append(queue)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(entry)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            if queue in _log_subscribers:
                _log_subscribers.remove(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
