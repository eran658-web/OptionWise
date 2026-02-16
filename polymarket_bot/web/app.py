"""FastAPI web dashboard for the Polymarket trading bot."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Module-level state — set by the entry point before the server starts
_bot = None  # TradingBot instance
_settings = None  # Settings instance
_bot_thread: threading.Thread | None = None
_bot_error: str = ""

# Log buffer for streaming to web UI
_log_buffer: deque = deque(maxlen=1000)

WEB_DIR = Path(__file__).parent

app = FastAPI(title="OptionWise Dashboard")
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=WEB_DIR / "templates")


# ---------------------------------------------------------------------------
# Log handler that captures records for the web UI
# ---------------------------------------------------------------------------

class WebLogHandler(logging.Handler):
    def emit(self, record):
        try:
            _log_buffer.append({
                "timestamp": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "name": record.name,
                "message": record.getMessage(),
            })
        except Exception:
            pass


def setup_web_logging():
    handler = WebLogHandler()
    handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(handler)


# ---------------------------------------------------------------------------
# Bot lifecycle helpers
# ---------------------------------------------------------------------------

def set_bot(bot, settings):
    global _bot, _settings
    _bot = bot
    _settings = settings


def start_bot_thread():
    global _bot_thread, _bot_error
    if _bot is None:
        return False
    if _bot_thread and _bot_thread.is_alive():
        return False

    _bot_error = ""

    def _run():
        global _bot_error
        try:
            _bot.initialize()
            _bot.run()
        except Exception as exc:
            _bot_error = str(exc)
            logging.getLogger("dashboard").error("Bot error: %s", exc, exc_info=True)

    _bot_thread = threading.Thread(target=_run, daemon=True, name="trading-bot")
    _bot_thread.start()
    return True


def stop_bot():
    if _bot:
        _bot._running = False


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status():
    if _bot is None:
        return {"running": False, "initialized": False}
    uptime = time.time() - _bot._start_time if _bot._start_time else 0
    return {
        "running": _bot._running,
        "dry_run": _bot.settings.bot.dry_run,
        "uptime_sec": uptime,
        "cycle_count": _bot._cycle_count,
        "markets_loaded": len(_bot._markets),
        "strategies_count": len(_bot.strategies),
        "error": _bot_error,
    }


@app.get("/api/dashboard")
async def api_dashboard():
    if _bot is None:
        return {"error": "Bot not initialized"}
    summary = _bot.risk_manager.get_summary()
    uptime = time.time() - _bot._start_time if _bot._start_time else 0
    return {
        "running": _bot._running,
        "dry_run": _bot.settings.bot.dry_run,
        "uptime_sec": uptime,
        "cycle_count": _bot._cycle_count,
        "markets_count": len(_bot._markets),
        "error": _bot_error,
        **summary,
    }


@app.get("/api/positions")
async def api_positions():
    if _bot is None:
        return []
    result = []
    for token_id, pos in list(_bot.risk_manager._positions.items()):
        result.append({
            "token_id": token_id,
            "market_id": pos.market_id,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "current_price": pos.current_price,
            "size_usd": pos.size_usd,
            "pnl": pos.pnl,
            "strategy": pos.strategy,
            "timestamp": pos.timestamp,
        })
    return result


@app.get("/api/strategies")
async def api_strategies():
    if _bot is None:
        return []

    cfgs = {
        "arbitrage": (
            _bot.settings.arbitrage,
            ["min_profit_pct", "scan_interval_sec", "max_position_per_arb_usd"],
        ),
        "market_making": (
            _bot.settings.market_making,
            [
                "spread_bps", "order_size_usd", "max_inventory_usd",
                "refresh_interval_sec", "min_liquidity_usd", "max_price_drift_pct",
            ],
        ),
        "value": (
            _bot.settings.value,
            [
                "scan_interval_sec", "min_volume_usd", "min_liquidity_usd",
                "lookback_trades", "mean_reversion_threshold_pct",
                "momentum_window", "max_position_per_trade_usd",
            ],
        ),
    }

    result = []
    seen = set()
    for s in _bot.strategies:
        seen.add(s.name)
        cfg_obj, keys = cfgs.get(s.name, (None, []))
        params = {k: getattr(cfg_obj, k) for k in keys} if cfg_obj else {}
        result.append({
            "name": s.name,
            "active": s._active,
            "enabled": getattr(cfg_obj, "enabled", False) if cfg_obj else False,
            "params": params,
        })

    for name, (cfg_obj, keys) in cfgs.items():
        if name not in seen:
            result.append({
                "name": name,
                "active": False,
                "enabled": cfg_obj.enabled,
                "params": {k: getattr(cfg_obj, k) for k in keys},
            })

    return result


@app.post("/api/strategies/{name}/toggle")
async def api_toggle_strategy(name: str):
    if _bot is None:
        return {"error": "Bot not initialized"}

    cfg_map = {
        "arbitrage": _bot.settings.arbitrage,
        "market_making": _bot.settings.market_making,
        "value": _bot.settings.value,
    }
    cfg = cfg_map.get(name)
    if cfg is None:
        return {"error": f"Unknown strategy: {name}"}

    cfg.enabled = not cfg.enabled
    for s in _bot.strategies:
        if s.name == name:
            s._active = cfg.enabled
    return {"name": name, "enabled": cfg.enabled}


@app.post("/api/strategies/{name}/params")
async def api_update_strategy_params(name: str, request: Request):
    if _bot is None:
        return {"error": "Bot not initialized"}

    data = await request.json()
    cfg_map = {
        "arbitrage": _bot.settings.arbitrage,
        "market_making": _bot.settings.market_making,
        "value": _bot.settings.value,
    }
    cfg = cfg_map.get(name)
    if cfg is None:
        return {"error": f"Unknown strategy: {name}"}

    for key, value in data.items():
        if hasattr(cfg, key):
            try:
                setattr(cfg, key, type(getattr(cfg, key))(value))
            except (TypeError, ValueError):
                pass
    return {"updated": True}


@app.get("/api/trades")
async def api_trades():
    if _bot is None:
        return []
    return list(reversed(_bot.risk_manager._trade_log))


@app.get("/api/config")
async def api_get_config():
    if _bot is None:
        return {}
    s = _bot.settings
    return {
        "risk": {
            "max_position_size_usd": s.risk.max_position_size_usd,
            "max_total_exposure_usd": s.risk.max_total_exposure_usd,
            "max_positions": s.risk.max_positions,
            "max_loss_per_trade_usd": s.risk.max_loss_per_trade_usd,
            "max_daily_loss_usd": s.risk.max_daily_loss_usd,
            "max_drawdown_pct": s.risk.max_drawdown_pct,
            "kelly_fraction": s.risk.kelly_fraction,
            "min_edge_pct": s.risk.min_edge_pct,
            "stop_loss_pct": s.risk.stop_loss_pct,
        },
        "bot": {
            "dry_run": s.bot.dry_run,
            "log_level": s.bot.log_level,
            "heartbeat_interval_sec": s.bot.heartbeat_interval_sec,
            "market_refresh_interval_sec": s.bot.market_refresh_interval_sec,
            "min_market_liquidity_usd": s.bot.min_market_liquidity_usd,
        },
    }


@app.post("/api/config")
async def api_update_config(request: Request):
    if _bot is None:
        return {"error": "Bot not initialized"}
    data = await request.json()
    section_map = {"risk": _bot.settings.risk, "bot": _bot.settings.bot}

    for section_name, params in data.items():
        obj = section_map.get(section_name)
        if obj is None:
            continue
        for key, value in params.items():
            if hasattr(obj, key):
                try:
                    setattr(obj, key, type(getattr(obj, key))(value))
                except (TypeError, ValueError):
                    pass

    # Keep clob dry_run flag in sync
    _bot.clob._dry_run = _bot.settings.bot.dry_run
    return {"updated": True}


@app.post("/api/bot/start")
async def api_bot_start():
    if _bot is None:
        return {"error": "Bot not initialized"}
    if _bot._running:
        return {"error": "Bot is already running"}
    return {"started": start_bot_thread()}


@app.post("/api/bot/stop")
async def api_bot_stop():
    if _bot is None:
        return {"error": "Bot not initialized"}
    stop_bot()
    return {"stopped": True}


@app.post("/api/bot/toggle-dry-run")
async def api_toggle_dry_run():
    if _bot is None:
        return {"error": "Bot not initialized"}
    _bot.settings.bot.dry_run = not _bot.settings.bot.dry_run
    _bot.clob._dry_run = _bot.settings.bot.dry_run
    return {"dry_run": _bot.settings.bot.dry_run}


@app.get("/api/logs")
async def api_logs(limit: int = 200):
    return list(_log_buffer)[-limit:]


@app.get("/api/markets")
async def api_markets():
    if _bot is None:
        return []
    result = []
    for m in _bot._markets[:50]:
        result.append({
            "id": m.id,
            "question": m.question,
            "outcomes": m.outcomes,
            "outcome_prices": m.outcome_prices,
            "volume": m.volume,
            "liquidity": m.liquidity,
        })
    return result


# ---------------------------------------------------------------------------
# WebSocket for live updates
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        for ws in list(self.active):
            try:
                await ws.send_json(data)
            except Exception:
                self.disconnect(ws)


_ws_manager = ConnectionManager()


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await _ws_manager.connect(websocket)
    last_log_idx = len(_log_buffer)

    try:
        while True:
            # Dashboard data
            if _bot is not None:
                summary = _bot.risk_manager.get_summary()
                uptime = time.time() - _bot._start_time if _bot._start_time else 0
                await websocket.send_json({
                    "type": "dashboard",
                    "data": {
                        "running": _bot._running,
                        "dry_run": _bot.settings.bot.dry_run,
                        "uptime_sec": uptime,
                        "cycle_count": _bot._cycle_count,
                        "markets_count": len(_bot._markets),
                        "error": _bot_error,
                        **summary,
                    },
                })

                # Positions
                positions = []
                for tid, pos in list(_bot.risk_manager._positions.items()):
                    positions.append({
                        "token_id": tid,
                        "market_id": pos.market_id,
                        "side": pos.side,
                        "entry_price": pos.entry_price,
                        "current_price": pos.current_price,
                        "size_usd": pos.size_usd,
                        "pnl": pos.pnl,
                        "strategy": pos.strategy,
                    })
                await websocket.send_json({"type": "positions", "data": positions})
            else:
                await websocket.send_json({
                    "type": "dashboard",
                    "data": {"running": False, "error": _bot_error},
                })

            # New log entries
            current_logs = list(_log_buffer)
            if len(current_logs) > last_log_idx:
                await websocket.send_json({
                    "type": "logs",
                    "data": current_logs[last_log_idx:],
                })
                last_log_idx = len(current_logs)

            await asyncio.sleep(2)
    except WebSocketDisconnect:
        _ws_manager.disconnect(websocket)
    except Exception:
        _ws_manager.disconnect(websocket)
