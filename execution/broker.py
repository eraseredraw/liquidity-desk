"""broker.py — abstract broker interface + MT5 connector.

Usage:
    from execution.broker import get_broker
    broker = get_broker()
    broker.connect()
    order_id = broker.place_order("GBPJPY", "buy", 216.5, sl=215.8, tp=218.0)
    positions = broker.get_positions()
    broker.disconnect()
"""

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class Order:
    symbol: str
    side: str  # buy / sell
    volume: float
    entry: float
    sl: float
    tp: float
    order_id: str = ""
    status: str = "pending"  # pending | open | filled | rejected | cancelled
    strategy: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    filled_at: Optional[str] = None
    comment: str = ""


@dataclass
class Position:
    symbol: str
    side: str
    volume: float
    entry: float
    current_price: float
    sl: float
    tp: float
    position_id: str
    unrealized_r: float = 0.0
    opened_at: Optional[str] = None


class Broker(ABC):
    @abstractmethod
    def connect(self) -> bool: ...
    @abstractmethod
    def disconnect(self) -> None: ...
    @abstractmethod
    def is_connected(self) -> bool: ...
    @abstractmethod
    def place_order(self, order: Order) -> Optional[str]: ...
    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...
    @abstractmethod
    def get_positions(self) -> list[Position]: ...
    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Position]: ...
    @abstractmethod
    def modify_position(self, position_id: str, sl: float = None, tp: float = None) -> bool: ...
    @abstractmethod
    def close_position(self, position_id: str) -> bool: ...
    @abstractmethod
    def get_account_info(self) -> dict: ...


# ── MT5 Broker ──────────────────────────────────────────────────────────────

class MT5Broker(Broker):
    def __init__(self, server: str = "", login: int = 0, password: str = "",
                 path: str = ""):
        self.server = server
        self.login = login
        self.password = password
        self.path = path
        self._connected = False
        self._mt5 = None

    def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5
            self._mt5 = mt5
            if self.path:
                ok = mt5.initialize(path=self.path)
            else:
                ok = mt5.initialize()
            if not ok:
                return False
            if self.login:
                authorized = mt5.login(login=self.login, server=self.server,
                                       password=self.password)
                if not authorized:
                    return False
            self._connected = True
            return True
        except ImportError:
            return False
        except Exception:
            return False

    def disconnect(self) -> None:
        if self._mt5:
            self._mt5.shutdown()
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def _ensure(self):
        if not self._connected:
            raise RuntimeError("MT5 not connected")

    def place_order(self, order: Order) -> Optional[str]:
        self._ensure()
        order_type = (self._mt5.ORDER_TYPE_BUY if order.side == "buy"
                      else self._mt5.ORDER_TYPE_SELL)
        symbol_info = self._mt5.symbol_info(order.symbol)
        if not symbol_info:
            return None
        point = symbol_info.point
        sl = round(order.sl / point) * point if order.sl else 0.0
        tp = round(order.tp / point) * point if order.tp else 0.0
        price = self._mt5.symbol_info_tick(order.symbol).ask if order.side == "buy" else self._mt5.symbol_info_tick(order.symbol).bid
        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": order.volume,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 10,
            "magic": 234000,
            "comment": order.comment or "bot",
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }
        result = self._mt5.order_send(request)
        if result and result.retcode == self._mt5.TRADE_RETCODE_DONE:
            return str(result.order)
        return None

    def cancel_order(self, order_id: str) -> bool:
        self._ensure()
        return self._mt5.order_delete(int(order_id)) if order_id.isdigit() else False

    def get_positions(self) -> list[Position]:
        self._ensure()
        positions = []
        for p in self._mt5.positions_get():
            pos = Position(
                symbol=p.symbol, side="buy" if p.type == 0 else "sell",
                volume=p.volume, entry=p.price_open,
                current_price=p.price_current, sl=p.sl, tp=p.tp,
                position_id=str(p.ticket),
                opened_at=datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat() if p.time else None,
            )
            risk = abs(p.price_open - p.sl) if p.sl else 0.0
            if risk > 0:
                pos.unrealized_r = (p.profit / (p.volume * risk)) if p.volume else 0.0
            positions.append(pos)
        return positions

    def get_position(self, symbol: str) -> Optional[Position]:
        for p in self.get_positions():
            if p.symbol == symbol:
                return p
        return None

    def modify_position(self, position_id: str, sl: float = None, tp: float = None) -> bool:
        self._ensure()
        request = {"position": int(position_id)}
        if sl is not None:
            request["sl"] = sl
        if tp is not None:
            request["tp"] = tp
        result = self._mt5.order_send({
            "action": self._mt5.TRADE_ACTION_SLTP,
            **request,
        })
        return bool(result and result.retcode == self._mt5.TRADE_RETCODE_DONE)

    def close_position(self, position_id: str) -> bool:
        self._ensure()
        pos = self._mt5.positions_get(ticket=int(position_id))
        if not pos:
            return False
        pos = pos[0]
        price = self._mt5.symbol_info_tick(pos.symbol).bid if pos.type == 0 else self._mt5.symbol_info_tick(pos.symbol).ask
        order_type = self._mt5.ORDER_TYPE_SELL if pos.type == 0 else self._mt5.ORDER_TYPE_BUY
        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": order_type,
            "position": pos.ticket,
            "price": price,
            "deviation": 10,
            "magic": 234000,
            "comment": "bot_close",
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }
        result = self._mt5.order_send(request)
        return bool(result and result.retcode == self._mt5.TRADE_RETCODE_DONE)

    def get_account_info(self) -> dict:
        self._ensure()
        info = self._mt5.account_info()
        if not info:
            return {}
        return {
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "margin_free": info.margin_free,
            "margin_level": info.margin_level,
            "currency": info.currency,
            "name": info.name,
            "server": info.server,
            "login": info.login,
        }


# ── Dry-Run Broker ──────────────────────────────────────────────────────────

class DryRunBroker(Broker):
    def __init__(self, journal_path: Optional[Path] = None):
        self._connected = True
        self._orders: list[Order] = []
        self._positions: dict[str, Position] = {}
        self._journal_path = journal_path or Path(__file__).parent.parent / "journal.json"
        self._journal = self._load_journal()

    def _load_journal(self) -> list:
        if self._journal_path.exists():
            import json
            try:
                return json.loads(self._journal_path.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save_journal(self) -> None:
        import json
        self._journal_path.write_text(
            json.dumps(self._journal, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass

    def is_connected(self) -> bool:
        return True

    def place_order(self, order: Order) -> Optional[str]:
        import uuid
        order.order_id = f"dry_{uuid.uuid4().hex[:12]}"
        order.status = "filled"
        order.filled_at = datetime.now(timezone.utc).isoformat()
        self._orders.append(order)
        pos = Position(
            symbol=order.symbol, side=order.side, volume=order.volume,
            entry=order.entry, current_price=order.entry,
            sl=order.sl, tp=order.tp, position_id=order.order_id,
            opened_at=order.filled_at,
        )
        self._positions[order.symbol] = pos
        entry = {
            "id": len(self._journal) + 1,
            "opened": order.filled_at,
            "symbol": order.symbol,
            "side": order.side,
            "entry": order.entry,
            "sl": order.sl,
            "tp": order.tp,
            "strategy": order.strategy,
            "status": "open",
        }
        self._journal.append(entry)
        self._save_journal()
        return order.order_id

    def cancel_order(self, order_id: str) -> bool:
        self._orders = [o for o in self._orders if o.order_id != order_id]
        return True

    def get_positions(self) -> list[Position]:
        now = datetime.now(timezone.utc)
        for pos in list(self._positions.values()):
            risk = abs(pos.entry - pos.sl) if pos.sl else 0.0
            if risk > 0:
                if pos.side == "buy":
                    pos.unrealized_r = (pos.current_price - pos.entry) / risk
                else:
                    pos.unrealized_r = (pos.entry - pos.current_price) / risk
        return list(self._positions.values())

    def get_position(self, symbol: str) -> Optional[Position]:
        pos = self._positions.get(symbol)
        if pos:
            risk = abs(pos.entry - pos.sl) if pos.sl else 0.0
            if risk > 0:
                pos.unrealized_r = (
                    (pos.current_price - pos.entry) / risk
                    if pos.side == "buy"
                    else (pos.entry - pos.current_price) / risk
                )
        return pos

    def update_price(self, symbol: str, price: float) -> None:
        if symbol in self._positions:
            self._positions[symbol].current_price = price

    def modify_position(self, position_id: str, sl: float = None, tp: float = None) -> bool:
        for pos in self._positions.values():
            if pos.position_id == position_id:
                if sl is not None:
                    pos.sl = sl
                if tp is not None:
                    pos.tp = tp
                return True
        return False

    def close_position(self, position_id: str) -> bool:
        for sym, pos in list(self._positions.items()):
            if pos.position_id == position_id:
                for entry in self._journal:
                    if entry.get("id") == int(position_id.split("_")[-1]) if position_id.split("_")[-1].isdigit() else False:
                        pass
                del self._positions[sym]
                return True
        return False

    def get_account_info(self) -> dict:
        total_r = sum(
            (e.get("r", 0.0) for e in self._journal if e.get("status") in ("win", "loss")),
        )
        return {
            "balance": 10000.0 + total_r * 100,  # sim
            "equity": 10000.0 + total_r * 100,
            "margin": 0.0,
            "margin_free": 10000.0 + total_r * 100,
            "margin_level": 0.0,
            "currency": "USD",
            "name": "Dry Run",
            "server": "paper",
            "login": 0,
        }


# ── Factory ─────────────────────────────────────────────────────────────────

_BROKER_INSTANCE = None


def get_broker(broker_type: str = "auto", **kwargs) -> Broker:
    global _BROKER_INSTANCE
    if _BROKER_INSTANCE is not None:
        return _BROKER_INSTANCE

    if broker_type == "dry":
        _BROKER_INSTANCE = DryRunBroker()
        return _BROKER_INSTANCE

    if broker_type == "mt5":
        _BROKER_INSTANCE = MT5Broker(**kwargs)
        return _BROKER_INSTANCE

    if broker_type == "auto":
        mt5 = MT5Broker(**kwargs)
        if mt5.connect():
            _BROKER_INSTANCE = mt5
            return _BROKER_INSTANCE
        dry = DryRunBroker()
        print("[broker] MT5 not available, falling back to DryRun")
        _BROKER_INSTANCE = dry
        return _BROKER_INSTANCE

    raise ValueError(f"Unknown broker type: {broker_type}")