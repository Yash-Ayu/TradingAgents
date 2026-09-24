"""Launch the local paper desk or run one offline/read-only cycle."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .angel_data import AngelReadOnlyFeed, DemoFeed
from .dashboard_server import DashboardServer
from .market_snapshot import InstrumentResolver, SessionCalendar
from .paper_ledger import PaperLedger
from .paper_service import PaperTradingService


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='TradingAgents paper desk (no live execution)')
    parser.add_argument('--health', action='store_true', help='Print startup capabilities and exit')
    parser.add_argument('--source', choices=['demo', 'angel'], default='demo')
    parser.add_argument('--host', default=None, help='Server bind host (default: 127.0.0.1 or $HOST env)')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--db', type=Path, help='Persistent SQLite ledger (separate demo/Angel defaults)')
    parser.add_argument('--interval', type=float, default=60)
    parser.add_argument('--cash', type=float, default=100000, help='Initial paper cash for a new ledger')
    parser.add_argument('--master', type=Path, help='Angel instrument master JSON')
    parser.add_argument('--calendar', type=Path, help='Verified exchange year/holidays/special_sessions JSON')
    parser.add_argument('--exchange', default='NSE')
    parser.add_argument('--symbol', help='Exact tradable symbol in the instrument master')
    parser.add_argument('--vix-symbol', help='Exact India VIX symbol in the instrument master')
    parser.add_argument('--analysis-symbol', help='Research ticker corresponding to the selected instrument')
    parser.add_argument('--engine', action='store_true', help='Enable configured LLM research (API usage)')
    parser.add_argument('--once', action='store_true', help='Run one cycle and exit')
    parser.add_argument('--max-order-value', type=float, default=10000)
    parser.add_argument('--max-order-qty', type=int, default=50)
    args = parser.parse_args(argv)
    if args.health:
        print(json.dumps({'status': 'ready', 'mode': 'paper', 'sources': ['demo', 'angel'],
                          'live_execution': False}))
        return 0
    if args.engine and not args.analysis_symbol:
        parser.error('--engine requires an explicit --analysis-symbol')
    calendar = None
    if args.source == 'demo':
        feed = DemoFeed()
    else:
        if not all([args.master, args.calendar, args.symbol, args.vix_symbol]):
            parser.error('Angel requires --master, --calendar, --symbol and --vix-symbol')
        from dotenv import load_dotenv
        load_dotenv()
        resolver = InstrumentResolver(json.loads(args.master.read_text(encoding='utf-8-sig')))
        instrument = resolver.resolve(args.exchange, args.symbol)
        vix = resolver.resolve('NSE', args.vix_symbol)
        feed = AngelReadOnlyFeed(instrument, vix)
        calendar = SessionCalendar(json.loads(args.calendar.read_text(encoding='utf-8-sig')))
    graph_factory = None
    if args.engine:
        def graph_factory():
            from tradingagents.graph.trading_graph import TradingAgentsGraph
            return TradingAgentsGraph()
    ledger = PaperLedger(args.db or Path('runtime_state') / f'{args.source}.sqlite3', args.cash)
    service = PaperTradingService(feed, ledger, graph_factory=graph_factory,
                                  analysis_symbol=args.analysis_symbol, calendar=calendar,
                                  interval=args.interval, max_order_value=args.max_order_value,
                                  max_order_qty=args.max_order_qty)
    if args.once:
        try:
            service.start(background=False)
            result = service.tick()
            print(json.dumps(result))
            return 1 if result.get('status') == 'error' else 0
        finally:
            service.close()
    server = DashboardServer(service, host=args.host, port=args.port)
    print(f'Paper desk: {server.origin} | source={args.source} | starts paused', flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
