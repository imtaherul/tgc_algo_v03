"""
FastAPI Trading Server — Binance USDT-M Futures
Run: uvicorn main:app --reload
"""
import time
import logging
import asyncio
import json
from datetime import datetime
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, StreamingResponse
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

def push_log(level: str, msg: str):
    entry = {"ts": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg}
    _terminal_logs.append(entry)
    if len(_terminal_logs) > 200:
        _terminal_logs.pop(0)
    for q in list(_log_subscribers):
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            pass

# ── Shared config ──────────────────────────────────────────────
USE_TESTNET  = False
SYMBOL       = "BTCUSDT"
LEVERAGE     = 10
AMOUNT       = 2000.0
TP_OFFSET    = 1000.0
SL_OFFSET    = 300.0
WORKING_TYPE = "MARK_PRICE"
POLL_INTERVAL = 10

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

def run_order(side: str, limit_price: float, amount: float,
              tp_offset: float, sl_offset: float) -> dict:
    client = get_client()
    quantity    = amount * LEVERAGE / limit_price
    take_profit = limit_price + tp_offset if side == "BUY" else limit_price - tp_offset
    stop_loss   = limit_price - sl_offset if side == "BUY" else limit_price + sl_offset
    log = []

    push_log("INFO", "─" * 40)
    push_log("INFO", f"New {side} order initiated")
    push_log("INFO", f"Symbol: {SYMBOL} | Leverage: {LEVERAGE}x | Margin: ${amount} | TP±{tp_offset} | SL±{sl_offset}")

    # Set leverage
    resp = client.change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
    msg = f"Leverage set to {resp['leverage']}x for {resp['symbol']}"
    log.append(f"✓ {msg}"); push_log("SUCCESS", msg)

    # Entry limit order
    push_log("INFO", f"Placing LIMIT {side} @ {fmt_price(limit_price)} | qty: {fmt_qty(quantity)}")
    entry = client.new_order(
        symbol=SYMBOL, side=side, type="LIMIT",
        timeInForce="GTC", quantity=fmt_qty(quantity), price=fmt_price(limit_price),
    )
    msg = f"LIMIT {side} placed — orderId: {entry['orderId']}"
    log.append(f"✓ {msg}"); push_log("SUCCESS", msg)

    # Wait for fill
    push_log("INFO", f"Waiting for order {entry['orderId']} to fill...")
    log.append(f"⏳ Waiting for order {entry['orderId']} to fill...")
    while True:
        order = client.query_order(symbol=SYMBOL, orderId=entry["orderId"])
        status = order["status"]
        push_log("INFO", f"Order {entry['orderId']} status: {status}")
        if status == "FILLED":
            msg = f"Order {entry['orderId']} FILLED!"
            log.append(f"✓ {msg}"); push_log("SUCCESS", msg); break
        if status in ("CANCELED", "REJECTED", "EXPIRED"):
            push_log("ERROR", f"Order ended with status: {status}")
            raise RuntimeError(f"Order ended with status: {status}")
        time.sleep(POLL_INTERVAL)

    # Take profit — NOTE: TAKE_PROFIT_MARKET does NOT accept timeInForce
    push_log("INFO", f"Placing TAKE_PROFIT_MARKET {exit_side(side)} @ {fmt_price(take_profit)}")
    tp = client.new_order(
        symbol=SYMBOL,
        side=exit_side(side),
        type="TAKE_PROFIT_MARKET",
        stopPrice=fmt_price(take_profit),
        closePosition="true",
        workingType=WORKING_TYPE,
        priceProtect="TRUE",
    )
    msg = f"TAKE_PROFIT_MARKET placed @ {fmt_price(take_profit)} — orderId: {tp['orderId']}"
    log.append(f"✓ {msg}"); push_log("SUCCESS", msg)

    # Stop loss — NOTE: STOP_MARKET does NOT accept timeInForce
    push_log("INFO", f"Placing STOP_MARKET {exit_side(side)} @ {fmt_price(stop_loss)}")
    sl = client.new_order(
        symbol=SYMBOL,
        side=exit_side(side),
        type="STOP_MARKET",
        stopPrice=fmt_price(stop_loss),
        closePosition="true",
        workingType=WORKING_TYPE,
        priceProtect="TRUE",
    )
    msg = f"STOP_MARKET placed @ {fmt_price(stop_loss)} — orderId: {sl['orderId']}"
    log.append(f"✓ {msg}"); push_log("SUCCESS", msg)

    push_log("SUCCESS", f"All orders placed! Entry:{entry['orderId']} TP:{tp['orderId']} SL:{sl['orderId']}")
    push_log("INFO", "─" * 40)

    return {
        "success": True, "log": log,
        "entry_id": entry["orderId"], "tp_id": tp["orderId"], "sl_id": sl["orderId"],
        "tp_price": fmt_price(take_profit), "sl_price": fmt_price(stop_loss),
        "qty": fmt_qty(quantity),
    }


# ── Routes ─────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    push_log("INFO", "Dashboard loaded")
    return templates.TemplateResponse("index.html", tpl_ctx(request))

@app.post("/buy", response_class=HTMLResponse)
async def buy(
    request: Request,
    limit_price: float = Form(...),
    margin:      float = Form(default=0),
    tp_offset:   float = Form(default=0),
    sl_offset:   float = Form(default=0),
):
    amount = margin    if margin    > 0 else AMOUNT
    tp     = tp_offset if tp_offset > 0 else TP_OFFSET
    sl     = sl_offset if sl_offset > 0 else SL_OFFSET
    try:
        result = run_order("BUY", limit_price, amount, tp, sl)
        return templates.TemplateResponse("index.html", tpl_ctx(request, {
            "result": result, "action": "BUY", "limit_price": limit_price,
            "cfg_amount": amount, "cfg_tp": tp, "cfg_sl": sl,
        }))
    except (ClientError, RuntimeError, Exception) as e:
        push_log("ERROR", f"BUY order failed: {e}")
        return templates.TemplateResponse("index.html", tpl_ctx(request, {
            "error": str(e), "action": "BUY", "limit_price": limit_price,
            "cfg_amount": amount, "cfg_tp": tp, "cfg_sl": sl,
        }))

@app.post("/sell", response_class=HTMLResponse)
async def sell(
    request: Request,
    limit_price: float = Form(...),
    margin:      float = Form(default=0),
    tp_offset:   float = Form(default=0),
    sl_offset:   float = Form(default=0),
):
    amount = margin    if margin    > 0 else AMOUNT
    tp     = tp_offset if tp_offset > 0 else TP_OFFSET
    sl     = sl_offset if sl_offset > 0 else SL_OFFSET
    try:
        result = run_order("SELL", limit_price, amount, tp, sl)
        return templates.TemplateResponse("index.html", tpl_ctx(request, {
            "result": result, "action": "SELL", "limit_price": limit_price,
            "cfg_amount": amount, "cfg_tp": tp, "cfg_sl": sl,
        }))
    except (ClientError, RuntimeError, Exception) as e:
        push_log("ERROR", f"SELL order failed: {e}")
        return templates.TemplateResponse("index.html", tpl_ctx(request, {
            "error": str(e), "action": "SELL", "limit_price": limit_price,
            "cfg_amount": amount, "cfg_tp": tp, "cfg_sl": sl,
        }))

@app.get("/logs/stream")
async def log_stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
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
