"""MAC Vendors MCP Server — FastMCP tools for MAC address vendor lookups."""

import argparse
import logging
import logging.config
import re
import sys
import time

import httpx
from fastmcp import FastMCP

API_BASE = "https://api.macvendors.com"

mcp = FastMCP("MAC Vendors")
_http: httpx.Client | None = None
_last_request: float = 0.0

WRITE_TOOLS: list[str] = []


def _rate_limited_get(url: str) -> httpx.Response:
    """GET with 1 req/sec rate limiting for macvendors.com free tier."""
    global _last_request
    elapsed = time.time() - _last_request
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_request = time.time()
    return _http.get(url)


def _normalize_mac(mac: str) -> str:
    """Normalize MAC to colon-separated format for the API."""
    # Strip common separators, lowercase
    raw = re.sub(r"[:\-.]", "", mac.strip().lower())
    if len(raw) not in (6, 12):
        return mac  # Let the API handle validation
    # Format as colon-separated
    return ":".join(raw[i : i + 2] for i in range(0, len(raw), 2))


@mcp.tool
def macvendors_lookup(mac: str) -> dict[str, str]:
    """Look up the manufacturer/vendor of a device by its MAC address.

    Uses the macvendors.com API to identify the OUI (Organizationally Unique
    Identifier) from the first 3 octets of the MAC address.

    Note: Randomized MACs (Apple Private Wi-Fi Address, Android) won't
    resolve to a real vendor.

    Args:
        mac: MAC address in any format (e.g. "d4:57:63:72:1e:00",
             "D4-57-63-72-1E-00", "d457.6372.1e00", or just OUI "d4:57:63").

    Returns:
        A dictionary with ``mac``, ``vendor``, and ``found`` keys.
        May also include an ``error`` key if rate-limited.
    """
    normalized = _normalize_mac(mac)
    resp = _rate_limited_get(f"{API_BASE}/{normalized}")

    if resp.status_code == 404:
        return {"mac": normalized, "vendor": None, "found": False}
    if resp.status_code == 429:
        return {
            "mac": normalized,
            "vendor": None,
            "found": False,
            "error": "Rate limited — try again in a second",
        }

    resp.raise_for_status()
    vendor = resp.text.strip()
    return {"mac": normalized, "vendor": vendor, "found": True}


@mcp.tool
def macvendors_bulk_lookup(macs: list[str]) -> list[dict[str, str]]:
    """Look up vendors for multiple MAC addresses.

    Rate-limited to 1 request/second per macvendors.com free tier.
    For large batches, this will be slow.

    Args:
        macs: List of MAC addresses in any format.

    Returns:
        A list of dictionaries, each with ``mac``, ``vendor``, and
        ``found`` keys. May also include an ``error`` key if rate-limited.
    """
    results = []
    for mac in macs:
        normalized = _normalize_mac(mac)
        resp = _rate_limited_get(f"{API_BASE}/{normalized}")

        if resp.status_code == 404:
            results.append({"mac": normalized, "vendor": None, "found": False})
        elif resp.status_code == 429:
            results.append(
                {
                    "mac": normalized,
                    "vendor": None,
                    "found": False,
                    "error": "Rate limited",
                }
            )
        else:
            resp.raise_for_status()
            results.append(
                {"mac": normalized, "vendor": resp.text.strip(), "found": True}
            )

    return results


def main() -> None:
    """Main entry point for the MAC Vendors MCP server."""
    global _http

    parser = argparse.ArgumentParser(description="MAC Vendors MCP Server")
    parser.add_argument(
        "--transport", type=str, choices=["stdio", "http"], default="stdio"
    )
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        default=None,
        help="Run in read-only mode (hide write tools)",
    )
    args = parser.parse_args()

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "console": {
                    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "console",
                    "stream": "ext://sys.stderr",
                }
            },
            "root": {"level": args.log_level, "handlers": ["console"]},
        }
    )

    logger = logging.getLogger(__name__)
    logger.info("Starting MAC Vendors MCP Server")

    _http = httpx.Client(timeout=10.0)

    if args.read_only and WRITE_TOOLS:
        for name in WRITE_TOOLS:
            mcp.remove_tool(name)
        logger.info("Read-only mode: %d write tools removed", len(WRITE_TOOLS))

    try:
        if args.transport == "stdio":
            mcp.run(transport="stdio")
        else:
            mcp.run(transport="http", host=args.host, port=args.port)
    except Exception as e:
        logger.error("Failed to start MCP server: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
