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

# ── Suppress noisy polling routes from uvicorn access log ─────
class _SuppressPolling(logging.Filter):
    _MUTED = {"/ticker", "/open-orders", "/open-positions", "/logs/stream"}
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._MUTED)

logging.getLogger("uvicorn.access").addFilter(_SuppressPolling())

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
USE_TESTNET      = False
SYMBOL           = "BTCUSDT"
LEVERAGE         = 10
AMOUNT           = 5000.0
TP_OFFSET        = 1000.0
SL_OFFSET        = 300.0
WORKING_TYPE     = "MARK_PRICE"
POLL_INTERVAL    = 1
MONITOR_INTERVAL = 1

# ── Server time sync ───────────────────────────────────────────
_time_offset_ms: int = 0

def sync_server_time():
    global _time_offset_ms
    try:
        tmp = UMFutures()
        server_time     = tmp.time()["serverTime"]
        local_time      = int(time.time() * 1000)
        _time_offset_ms = server_time - local_time
        logger.info(f"Time sync OK — offset: {_time_offset_ms:+d}ms")
    except Exception as e:
        logger.warning(f"Time sync failed: {e}")

def get_client() -> UMFutures:
    if USE_TESTNET:
        return UMFutures(key=api, secret=secret, base_url="https://testnet.binancefuture.com")
    return UMFutures(key=api, secret=secret)

def signed_request(client: UMFutures, method: str, path: str, params: dict) -> dict:
    """Inject corrected timestamp + recvWindow into every signed request."""
    params["timestamp"]  = int(time.time() * 1000) + _time_offset_ms
    params["recvWindow"] = 60000
    return client.sign_request(method, path, params)

def fmt_price(p: float) -> str:
    rounded = round(round(p / 0.1) * 0.1, 1)
    return f"{rounded:.1f}"

def fmt_qty(q: float)   -> str: return f"{q:.3f}"
def exit_side(side: str) -> str: return "SELL" if side == "BUY" else "BUY"
# Hedge mode: BUY order = LONG position side, SELL order = SHORT position side
def position_side(side: str) -> str: return "LONG" if side == "BUY" else "SHORT"

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

# ── Position monitor ───────────────────────────────────────────
def _cancel_algo(client: UMFutures, algo_id: int, reason: str):
    try:
        signed_request(client, "DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
        push_log("WARN", f"🗑  Auto-cancelled algo #{algo_id}  ({reason})")
    except Exception as e:
        push_log("ERROR", f"✗  Failed to auto-cancel algo #{algo_id}: {e}")

def position_monitor():
    push_log("INFO", f"🔍 Position monitor started (every {MONITOR_INTERVAL}s)")
    client = get_client()
    prev_pos_size: float = None
    _sync_counter = 0

    while True:
        try:
            time.sleep(MONITOR_INTERVAL)
            _sync_counter += 1
            if _sync_counter % 1800 == 0:
                sync_server_time()
                client = get_client()

            positions = signed_request(client, "GET", "/fapi/v2/positionRisk", {"symbol": SYMBOL})
            pos_size = 0.0
            for p in (positions if isinstance(positions, list) else []):
                if p.get("symbol") == SYMBOL:
                    pos_size += abs(float(p.get("positionAmt", 0)))

            algo_resp = signed_request(client, "GET", "/fapi/v1/openAlgoOrders", {"symbol": SYMBOL})
            if isinstance(algo_resp, dict):
                algo_orders = (algo_resp.get("orders") or algo_resp.get("algoOrders") or algo_resp.get("data") or [])
            elif isinstance(algo_resp, list):
                algo_orders = algo_resp
            else:
                algo_orders = []

            if pos_size == 0.0 and algo_orders:
                push_log("WARN", "⚠  Position CLOSED — cancelling orphan algo orders...")
                for o in algo_orders:
                    algo_id   = o.get("algoId")
                    algo_type = o.get("orderType", o.get("type", "?"))
                    trigger   = o.get("triggerPrice", "?")
                    push_log("INFO", f"   Cancelling {algo_type} algo #{algo_id} trigger:${trigger}")
                    _cancel_algo(client, algo_id, "position closed")
                push_log("SUCCESS", "✓  All orphan algo orders cancelled")

            if prev_pos_size is not None:
                if prev_pos_size == 0.0 and pos_size > 0.0:
                    push_log("INFO", f"📈 Position OPENED — size: {pos_size} BTC")
                elif prev_pos_size > 0.0 and pos_size == 0.0:
                    push_log("SUCCESS", f"✅ Position CLOSED — was {prev_pos_size} BTC")

            prev_pos_size = pos_size

        except Exception as e:
            logger.warning(f"Position monitor error: {e}")


# ── Order execution (Hedge Mode) ───────────────────────────────
def run_order(side: str, limit_price: float, amount: float,
              tp_offset: float, sl_offset: float,
              sym: str = None, lev: int = None):
    # Use per-order symbol/leverage if provided, else fall back to global config
    sym      = sym or SYMBOL
    lev      = lev or LEVERAGE
    client   = get_client()
    quantity    = amount * lev / limit_price
    take_profit = limit_price + tp_offset if side == "BUY" else limit_price - tp_offset
    stop_loss   = limit_price - sl_offset if side == "BUY" else limit_price + sl_offset
    pos_side    = position_side(side)
    exit_s      = exit_side(side)

    push_log("INFO",  "─" * 44)
    push_log("INFO",  f"▶ Starting {side} order sequence [HEDGE MODE]")
    push_log("INFO",  f"  Symbol      : {sym}")
    push_log("INFO",  f"  PositionSide: {pos_side}")
    push_log("INFO",  f"  Margin      : ${amount} USDT  |  Leverage: {lev}x")
    push_log("INFO",  f"  Entry       : ${fmt_price(limit_price)}")
    push_log("INFO",  f"  Quantity    : {fmt_qty(quantity)} BTC")
    push_log("INFO",  f"  TP target   : ${fmt_price(take_profit)}  (+{tp_offset})")
    push_log("INFO",  f"  SL target   : ${fmt_price(stop_loss)}  (-{sl_offset})")
    push_log("INFO",  "─" * 44)

    try:
        # Step 1: Set leverage for the specific position side
        push_log("INFO", "⚙  [1/4] Setting leverage...")
        resp = client.change_leverage(symbol=sym, leverage=lev)
        push_log("SUCCESS", f"✓  Leverage set to {resp['leverage']}x for {resp['symbol']}")

        # Step 2: Place limit entry — hedge mode requires positionSide
        push_log("INFO", f"📤 [2/4] Placing LIMIT {side} {pos_side} @ ${fmt_price(limit_price)} | qty: {fmt_qty(quantity)}")
        entry = client.new_order(
            symbol=sym,
            side=side,
            positionSide=pos_side,      # ← hedge mode key field
            type="LIMIT",
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
            order  = client.query_order(symbol=sym, orderId=entry_id)
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

        # Step 4: Place TP + SL via Algo Order API — hedge mode needs positionSide
        push_log("INFO", "📤 [4/4] Placing TP & SL via Algo Order API...")
        tp_exec = take_profit - 5  if side == "BUY" else take_profit + 5
        sl_exec = stop_loss  - 10  if side == "BUY" else stop_loss  + 10

        push_log("INFO", f"   → TP: TAKE_PROFIT {exit_s} {pos_side} | trigger: ${fmt_price(take_profit)} | exec: ${fmt_price(tp_exec)}")
        tp = signed_request(client, "POST", "/fapi/v1/algoOrder", {
            "symbol":       sym,
            "side":         exit_s,
            "positionSide": pos_side,
            "algoType":     "CONDITIONAL",
            "type":         "TAKE_PROFIT",
            "quantity":     fmt_qty(quantity),
            "price":        fmt_price(tp_exec),
            "triggerPrice": fmt_price(take_profit),
            "timeInForce":  "GTC",
            "workingType":  WORKING_TYPE,
        })
        tp_id = tp.get("algoId", "?")
        push_log("SUCCESS", f"✓  Take Profit placed | trigger: ${fmt_price(take_profit)} — algoId: {tp_id}")

        push_log("INFO", f"   → SL: STOP {exit_s} {pos_side} | trigger: ${fmt_price(stop_loss)} | exec: ${fmt_price(sl_exec)}")
        sl = signed_request(client, "POST", "/fapi/v1/algoOrder", {
            "symbol":       sym,
            "side":         exit_s,
            "positionSide": pos_side,
            "algoType":     "CONDITIONAL",
            "type":         "STOP",
            "quantity":     fmt_qty(quantity),
            "price":        fmt_price(sl_exec),
            "triggerPrice": fmt_price(stop_loss),
            "timeInForce":  "GTC",
            "workingType":  WORKING_TYPE,
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
    sync_server_time()
    threading.Thread(target=position_monitor, daemon=True).start()
    def _open_browser():
        import webbrowser
        time.sleep(1.5)
        webbrowser.open("http://127.0.0.1:8000")
    threading.Thread(target=_open_browser, daemon=True).start()

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
    symbol:      str   = Form(default=""),
    leverage:    int   = Form(default=0),
):
    amount   = margin    if margin    > 0  else AMOUNT
    tp       = tp_offset if tp_offset > 0  else TP_OFFSET
    sl       = sl_offset if sl_offset > 0  else SL_OFFSET
    sym      = symbol.strip().upper()  if symbol.strip()  else SYMBOL
    lev      = leverage                if leverage >= 1   else LEVERAGE
    threading.Thread(target=run_order, args=(side, limit_price, amount, tp, sl, sym, lev), daemon=True).start()
    return JSONResponse({"status": "started", "side": side, "price": limit_price, "symbol": sym})

@app.get("/open-orders")
async def open_orders():
    client = get_client()
    regular = []
    algo    = []
    try:
        # r = signed_request(client, "GET", "/fapi/v1/openOrders", {"symbol": SYMBOL})
        r = signed_request(client, "GET", "/fapi/v1/openOrders", {})
        regular = r if isinstance(r, list) else []
    except Exception as e:
        push_log("ERROR", f"Failed to fetch regular orders: {e}")
    try:
        ar = signed_request(client, "GET", "/fapi/v1/openAlgoOrders", {})
        if isinstance(ar, dict):
            algo = ar.get("orders") or ar.get("algoOrders") or ar.get("data") or []
        elif isinstance(ar, list):
            algo = ar
    except Exception as e:
        push_log("ERROR", f"Failed to fetch algo orders: {e}")
    return JSONResponse({"regular": regular, "algo": algo})

@app.post("/cancel-order")
async def cancel_order(order_id: str = Form(...), order_type: str = Form(default="regular")):
    client = get_client()
    try:
        if order_type == "algo":
            signed_request(client, "DELETE", "/fapi/v1/algoOrder", {"algoId": int(order_id)})
            push_log("WARN", f"🗑  Algo order #{order_id} cancelled")
        else:
            client.cancel_order(symbol=SYMBOL, orderId=int(order_id))
            push_log("WARN", f"🗑  Order #{order_id} cancelled")
        return JSONResponse({"status": "cancelled", "orderId": order_id})
    except Exception as e:
        push_log("ERROR", f"✗  Cancel failed for #{order_id}: {e}")
        return JSONResponse({"status": "error", "msg": str(e)}, status_code=400)

@app.get("/open-positions")
async def open_positions():
    client = get_client()
    try:
        positions = signed_request(client, "GET", "/fapi/v2/positionRisk", {})
        # Hedge mode: filter both LONG and SHORT positions with non-zero size
        open_pos = [p for p in (positions if isinstance(positions, list) else [])
                    if abs(float(p.get("positionAmt", 0))) > 0]
        return JSONResponse({"positions": open_pos})
    except Exception as e:
        push_log("ERROR", f"Failed to fetch positions: {e}")
        return JSONResponse({"positions": [], "error": str(e)})

@app.post("/close-position")
async def close_position(
    symbol:        str = Form(...),
    side:          str = Form(...),
    quantity:      str = Form(...),
    position_side: str = Form(default="BOTH"),  # LONG, SHORT, or BOTH
):
    client = get_client()
    close_side = "SELL" if side == "BUY" else "BUY"
    try:
        result = client.new_order(
            symbol=symbol,
            side=close_side,
            positionSide=position_side,   # ← hedge mode key field
            type="MARKET",
            quantity=quantity,
        )
        push_log("WARN",    f"⚡ Position MARKET CLOSED — {position_side} {quantity} {symbol}")
        push_log("SUCCESS", f"✓  Close order filled — orderId: {result.get('orderId','?')}")
        return JSONResponse({"status": "closed", "orderId": result.get("orderId")})
    except Exception as e:
        push_log("ERROR", f"✗  Close position failed: {e}")
        return JSONResponse({"status": "error", "msg": str(e)}, status_code=400)

@app.get("/ticker")
async def ticker(symbol: str = SYMBOL):
    client = get_client()
    try:
        data  = client.mark_price(symbol=symbol)
        price = float(data.get("markPrice", 0))
        return JSONResponse({"price": price, "symbol": symbol})
    except Exception as e:
        return JSONResponse({"price": 0, "error": str(e)}, status_code=500)

@app.get("/logs/stream")
async def log_stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
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
