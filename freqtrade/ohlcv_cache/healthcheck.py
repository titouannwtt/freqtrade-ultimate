"""
Healthcheck CLI for both ftcache and ftpairlist daemons.

Usage:
    python -m freqtrade.ohlcv_cache.healthcheck            # ftcache status
    python -m freqtrade.ohlcv_cache.healthcheck --pairlist  # ftpairlist status
"""

from __future__ import annotations

import json
import socket
import sys


def _query_unix(sock_path: str, request: dict, timeout: float = 5.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        payload = (json.dumps(request, separators=(",", ":")) + "\n").encode()
        s.sendall(payload)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(1024 * 1024)
            if not chunk:
                raise ConnectionError("daemon closed connection")
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])
    finally:
        s.close()


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def ftcache_status(sock_path: str) -> int:
    try:
        ping = _query_unix(sock_path, {"op": "ping", "req_id": "hc"})
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"OFFLINE  ftcache daemon not running (socket: {sock_path})")
        return 1
    except TimeoutError:
        print(f"TIMEOUT  ftcache daemon not responding (socket: {sock_path})")
        return 2
    except Exception as e:
        print(f"ERROR    {e}")
        return 2

    if not ping.get("ok"):
        print(f"ERROR    unexpected ping response: {ping}")
        return 2

    try:
        stats = _query_unix(sock_path, {"op": "stats", "req_id": "hc-stats"})
    except Exception:
        stats = {}

    uptime = _format_duration(ping.get("uptime_s", 0))
    version = ping.get("daemon_version", "?")

    print(f"ONLINE   ftcache daemon (proto v{version}, uptime {uptime})")
    print(f"         socket: {sock_path}")

    if stats.get("ok"):
        total = stats.get("requests_total", 0)
        hits = stats.get("cache_hits", 0)
        partial = stats.get("cache_partial", 0)
        misses = stats.get("cache_misses", 0)
        errors = stats.get("fetch_errors", 0)
        rate = (hits / total * 100) if total > 0 else 0
        clients = stats.get("active_clients", 0)
        series = stats.get("series_count", 0)
        pending = stats.get("pending_fetches", 0)
        peak = stats.get("peak_pending", 0)

        print(f"         clients: {clients}  series: {series}")
        print(f"         requests: {total}  hit_rate: {rate:.1f}%")
        print(f"         hits: {hits}  partial: {partial}  misses: {misses}  errors: {errors}")
        print(f"         pending: {pending}  peak_pending: {peak}")

        # Rate limiter stats
        acquire = stats.get("acquire_total", 0)
        t_req = stats.get("tickers_requests", 0)
        t_hits = stats.get("tickers_cache_hits", 0)
        t_fetches = stats.get("tickers_fetches", 0)
        p_puts = stats.get("positions_puts", 0)
        p_gets = stats.get("positions_gets", 0)
        p_hits = stats.get("positions_cache_hits", 0)

        if acquire or t_req or p_gets:
            print(f"         rate_tokens: {acquire}")
            t_rate = (t_hits / t_req * 100) if t_req > 0 else 0
            print(
                f"         tickers: {t_req} req  {t_hits} hits"
                f" ({t_rate:.0f}%)  {t_fetches} fetches",
            )
            p_rate = (p_hits / p_gets * 100) if p_gets > 0 else 0
            print(
                f"         positions: {p_gets} gets  {p_hits} hits ({p_rate:.0f}%)  {p_puts} puts",
            )

    return 0


def ftpairlist_status(sock_path: str) -> int:
    try:
        ping = _query_unix(sock_path, {"op": "ping", "req_id": "hc"})
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"OFFLINE  pairlist cache daemon not running (socket: {sock_path})")
        return 1
    except TimeoutError:
        print(f"TIMEOUT  pairlist cache daemon not responding (socket: {sock_path})")
        return 2
    except Exception as e:
        print(f"ERROR    {e}")
        return 2

    if not ping.get("ok"):
        print(f"ERROR    unexpected ping response: {ping}")
        return 2

    try:
        stats = _query_unix(sock_path, {"op": "stats", "req_id": "hc-stats"})
    except Exception:
        stats = {}

    uptime = _format_duration(ping.get("uptime_s", 0))
    version = ping.get("version", "?")

    print(f"ONLINE   pairlist cache daemon (proto v{version}, uptime {uptime})")
    print(f"         socket: {sock_path}")

    if stats.get("ok"):
        gets = stats.get("gets", 0)
        hits_val = stats.get("hits", 0)
        puts = stats.get("puts", 0)
        entries = stats.get("entries", 0)
        clients = stats.get("clients", 0)
        rate = (hits_val / gets * 100) if gets > 0 else 0

        print(f"         clients: {clients}  entries: {entries}")
        print(f"         gets: {gets}  hits: {hits_val}  hit_rate: {rate:.1f}%")
        print(f"         puts: {puts}")

    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Check ftcache / ftpairlist daemon health",
    )
    parser.add_argument(
        "--pairlist",
        action="store_true",
        help="Check pairlist cache daemon instead of ftcache",
    )
    parser.add_argument(
        "--socket",
        default=None,
        help="Override socket path",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Output raw JSON stats",
    )
    args = parser.parse_args()

    if args.pairlist:
        from freqtrade.pairlist_cache.defaults import default_socket_path

        sock = args.socket or default_socket_path()
        if args.output_json:
            return _json_output(sock)
        return ftpairlist_status(sock)
    else:
        from freqtrade.ohlcv_cache.defaults import default_socket_path

        sock = args.socket or default_socket_path()
        if args.output_json:
            return _json_output(sock)
        return ftcache_status(sock)


def _json_output(sock_path: str) -> int:
    try:
        ping = _query_unix(sock_path, {"op": "ping", "req_id": "hc"})
        stats = _query_unix(sock_path, {"op": "stats", "req_id": "hc-stats"})
        result = {"online": True, "ping": ping, "stats": stats}
    except Exception as e:
        result = {"online": False, "error": str(e)}

    print(json.dumps(result, indent=2))
    return 0 if result.get("online") else 1


if __name__ == "__main__":
    sys.exit(main())
