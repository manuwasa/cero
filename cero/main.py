"""
Cero — entry point.

Boots every module as cooperating asyncio tasks in one process:
  - Exchange client (ccxt async + ws)
  - Price worker (backfill + watch candles)
  - Brain scheduler (re-evaluate on closed bars, dispatch to mode)
  - Risk gate (hydrated from DB so a restart doesn't un-trip)
  - Trip watcher (cancel + close on trip)
  - Notifier (Telegram if creds, else log)
  - Order placer (real ccxt placer, used by approval/auto modes)

Run with:
    uv run python -m cero
"""
from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from loguru import logger

import uvicorn

from cero.brain.risk import RiskGate
from cero.brain.scheduler import BrainScheduler
from cero.config import Config, Secrets, load_config
from cero.data.account_worker import AccountWorker
from cero.data.calendar_worker import CalendarWorker
from cero.data.exchange import ExchangeClient
from cero.data.news_worker import NewsWorker
from cero.data.price_worker import PriceWorker
from cero.db.session import close_db, init_db
from cero.exec.modes import (
    AutoMode,
    ExecutionMode,
    FilteredNotifier,
    LogNotifier,
    StubOrderPlacer,
    TripWatcher,
    build_mode,
)
from cero.exec.orders import CcxtOrderPlacer
from cero.exec.paper import PaperBroker
from cero.exec.protocols import Notifier, OrderPlacer
from cero.ui.telegram.bot import TelegramNotifier, build_notifier
from cero.ui.web.server import build_app


# ──────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────


def _setup_logging(cfg: Config) -> None:
    """Console + rotating file sink via loguru."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=cfg.logging.level,
        format=(
            "<green>{time:HH:mm:ss}</green> "
            "<level>{level: <7}</level> "
            "<cyan>{extra}</cyan> {message}"
        ),
        backtrace=False,
        diagnose=False,
    )
    log_path = Path(cfg.logging.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_path,
        level=cfg.logging.level,
        rotation=f"{cfg.logging.rotate_mb} MB",
        retention=cfg.logging.keep_files,
        compression="gz",
        enqueue=True,
    )


# ──────────────────────────────────────────────────────────────────────
# Wiring
# ──────────────────────────────────────────────────────────────────────


class Cero:
    """All long-lived objects in one place. Use start() / stop() to manage."""

    def __init__(self, cfg: Config, secrets: Secrets) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.exchange: ExchangeClient | None = None
        self.risk_gate: RiskGate | None = None
        self.notifier: Notifier | None = None
        self._notifier_impl: Notifier | None = None   # raw (unwrapped) for lifecycle
        self.placer: OrderPlacer | None = None
        self.paper_broker: PaperBroker | None = None
        self.mode: ExecutionMode | None = None
        self.price_worker: PriceWorker | None = None
        self.account_worker: AccountWorker | None = None
        self.calendar_worker: CalendarWorker | None = None
        self.news_worker: NewsWorker | None = None
        self.trip_watcher: TripWatcher | None = None
        self.scheduler: BrainScheduler | None = None
        self.web_server: uvicorn.Server | None = None
        self._web_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        cfg, secrets = self.cfg, self.secrets

        # 1. DB
        await init_db(cfg.database)

        # 2. Exchange
        self.exchange = ExchangeClient(cfg, secrets)
        await self.exchange.connect()

        # 3. Risk gate (hydrate any prior un-cleared trip)
        self.risk_gate = RiskGate(cfg.risk, cfg.news)
        await self.risk_gate.hydrate()

        # 4. Notifier — Telegram if creds present, log fallback otherwise
        services = {"config": cfg, "risk_gate": self.risk_gate}
        tg = build_notifier(
            secrets.telegram_bot_token, secrets.telegram_chat_id,
            services=services, backup_chat_id=secrets.telegram_chat_id_2,
        )
        if tg is not None:
            self._notifier_impl = tg
            await tg.start()
        else:
            self._notifier_impl = LogNotifier()
            logger.warning("Telegram not configured — using LogNotifier")
        # Wrap so config.alerts is actually honored (e.g. on_signal=false stops
        # the per-signal spam). The raw impl is kept for start/stop lifecycle.
        self.notifier = FilteredNotifier(self._notifier_impl, cfg.alerts)

        # 5+6. Placer + execution mode.
        #   paper        -> PaperBroker (simulated fills + PnL on live prices),
        #                   driven by AutoMode logic. NO real orders, ever.
        #   approval/auto -> real CcxtOrderPlacer (live money).
        #   signal_only  -> StubOrderPlacer (alerts only, no orders).
        if cfg.mode == "paper":
            self.paper_broker = PaperBroker(
                cfg, self.risk_gate, self.notifier,
                starting_equity=cfg.paper_equity,
            )
            self.placer = self.paper_broker
            self.mode = AutoMode(
                notifier=self.notifier, placer=self.placer, risk_gate=self.risk_gate,
            )
            self.paper_broker.start()
            logger.info(
                "mode=paper — PaperBroker simulating fills (equity={:.2f}, NO real money)",
                cfg.paper_equity,
            )
        elif cfg.mode in ("approval", "auto"):
            self.placer = CcxtOrderPlacer(self.exchange)
            self.mode = build_mode(
                cfg.mode, notifier=self.notifier, placer=self.placer,
                risk_gate=self.risk_gate,
            )
        else:
            self.placer = StubOrderPlacer()
            logger.info("mode={} — using StubOrderPlacer (no real orders)", cfg.mode)
            self.mode = build_mode(
                cfg.mode, notifier=self.notifier, placer=self.placer,
                risk_gate=self.risk_gate,
            )

        # 7. Trip watcher — listens for trip:fired, cancels + closes
        self.trip_watcher = TripWatcher(
            self.notifier, self.placer, cfg.symbols,
        )
        self.trip_watcher.start()

        # 8. Price worker — backfill + ws candle stream
        self.price_worker = PriceWorker(cfg, self.exchange)
        self.price_worker.start()

        # 8b. Account worker — polls balance + reconciles positions; TRIPs on
        #     unexpected positions (requires creds).
        #     SKIPPED in paper mode: paper positions live only in the DB (never
        #     on the exchange), so the reconciler would see them as "closed",
        #     delete them, and write fake `exit_reason='other'` trades — which
        #     corrupts paper results. The PaperBroker owns paper positions.
        if cfg.mode == "paper":
            logger.info("mode=paper — account_worker disabled (paper positions are local)")
        elif self.exchange.authenticated:
            self.account_worker = AccountWorker(cfg, self.exchange, self.risk_gate)
            self.account_worker.start()
        else:
            logger.warning("no API key — account_worker disabled")

        # 8c. Calendar worker — pulls upcoming events for news-blackout gating.
        #     Public feed, no creds required, runs every hour.
        self.calendar_worker = CalendarWorker(cfg)
        self.calendar_worker.start()

        # 8d. News worker — RSS scrape for dashboard context. Doesn't gate
        #     trading; idle if no feeds are configured.
        self.news_worker = NewsWorker(cfg)
        self.news_worker.start()

        # 9. Brain scheduler — fires on closed 5m bars. In paper mode it sizes
        #    against the simulated account equity instead of the exchange.
        self.scheduler = BrainScheduler(
            cfg, self.exchange, self.risk_gate, lambda: self.mode,  # type: ignore[arg-type]
            equity_provider=(
                (lambda: self.paper_broker.equity) if self.paper_broker else None
            ),
        )
        self.scheduler.start()

        # 10. Web dashboard — embedded uvicorn server task
        app = build_app(cfg, self.risk_gate, exchange=self.exchange)
        uconfig = uvicorn.Config(
            app, host=cfg.web.host, port=cfg.web.port,
            log_level="warning",       # noisy access logs aren't useful here
            access_log=False,
        )
        self.web_server = uvicorn.Server(uconfig)
        self._web_task = asyncio.create_task(self.web_server.serve(), name="web")
        logger.info("dashboard at http://{}:{}", cfg.web.host, cfg.web.port)

        logger.info(
            "Cero up — exchange={} testnet={} mode={} symbols={}",
            cfg.exchange.name, cfg.exchange.testnet, cfg.mode,
            ", ".join(cfg.symbols),
        )
        await self.notifier.send_notice(
            f"Cero online — {cfg.exchange.name} "
            f"({'testnet' if cfg.exchange.testnet else 'MAINNET'}), mode={cfg.mode}"
        )

    async def stop(self) -> None:
        """Reverse order. Best-effort: every stop call is wrapped so one
        failure doesn't prevent the rest from cleaning up."""
        logger.info("shutting down ...")
        for label, coro in (
            ("scheduler",       self.scheduler.stop()        if self.scheduler else None),
            ("paper_broker",    self.paper_broker.stop()     if self.paper_broker else None),
            ("account_worker",  self.account_worker.stop()   if self.account_worker else None),
            ("calendar_worker", self.calendar_worker.stop()  if self.calendar_worker else None),
            ("news_worker",     self.news_worker.stop()      if self.news_worker else None),
            ("price_worker",    self.price_worker.stop()     if self.price_worker else None),
            ("trip_watcher",    self.trip_watcher.stop()     if self.trip_watcher else None),
            ("web",             self._stop_web()),
            ("notifier",    self._stop_notifier()),
            ("exchange",    self.exchange.close()    if self.exchange else None),
            ("db",          close_db()),
        ):
            if coro is None:
                continue
            try:
                await coro
            except Exception as e:  # noqa: BLE001
                logger.warning("stop({}) failed: {}", label, e)
        logger.info("shutdown complete")

    async def _stop_web(self) -> None:
        if self.web_server is not None:
            self.web_server.should_exit = True
        if self._web_task is not None:
            try:
                await asyncio.wait_for(self._web_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                if self._web_task is not None:
                    self._web_task.cancel()

    async def _stop_notifier(self) -> None:
        impl = self._notifier_impl
        if isinstance(impl, TelegramNotifier):
            try:
                await impl.send_notice("Cero shutting down")
            except Exception:  # noqa: BLE001
                pass
            await impl.stop()

    async def wait_for_shutdown(self) -> None:
        await self._stop_event.wait()

    def request_shutdown(self) -> None:
        self._stop_event.set()


# ──────────────────────────────────────────────────────────────────────
# Entry points
# ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    cfg, secrets = load_config()
    _setup_logging(cfg)
    logger.info("Cero starting ...")

    app = Cero(cfg, secrets)

    # Install SIGINT/SIGTERM handlers so Ctrl+C and `docker stop` exit cleanly.
    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, app.request_shutdown)
    except NotImplementedError:
        # Windows ProactorEventLoop doesn't support add_signal_handler;
        # KeyboardInterrupt at the asyncio.run() level still cancels main().
        pass

    try:
        await app.start()
        await app.wait_for_shutdown()
    finally:
        await app.stop()


def run() -> None:
    """Console entry point: `python -m cero`."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("interrupted")
        sys.exit(0)


if __name__ == "__main__":
    run()
