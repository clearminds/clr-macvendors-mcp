"""MAC Vendors MCP Server — FastMCP tools for MAC address vendor lookups."""

import argparse
import logging
import logging.config
import os
import re
import sys
import time

import httpx
from fastmcp import FastMCP

from clr_macvendors_mcp.oui_db import build_db, list_brands, search_by_brand
from clr_macvendors_mcp.middleware import ToolValidationMiddleware

API_BASE = "https://api.macvendors.com"

mcp = FastMCP("MAC Vendors")
mcp.add_middleware(ToolValidationMiddleware())
_http: httpx.Client | None = None
_last_request: float = 0.0

WRITE_TOOLS: list[str] = ["update_oui_database"]


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


@mcp.tool
def search_brand_prefixes(brand: str) -> dict:
    """Search the IEEE OUI database for MAC prefixes belonging to a brand.

    Looks up all registered MAC address prefixes (OUI assignments) for a
    given manufacturer or organization name.  Uses fuzzy substring matching
    so partial names work (e.g. ``"Cisco"`` matches ``"Cisco Systems, Inc"``).

    The local OUI database is automatically initialised on first use by
    downloading IEEE CSV files (~10 seconds).

    Args:
        brand: Organization or manufacturer name to search for
            (e.g. ``"Cisco"``, ``"Nokia"``, ``"Dell"``).

    Returns:
        A dictionary with ``brand`` (the query), ``count`` (number of
        matching prefixes), and ``prefixes`` (list of dicts each with
        ``prefix``, ``registry``, ``organization``, and ``country``).
    """
    results = search_by_brand(brand)
    return {"brand": brand, "count": len(results), "prefixes": results}


@mcp.tool
def list_oui_brands(query: str | None = None) -> dict:
    """List organizations (brands) in the IEEE OUI database.

    Without a query, returns the top 100 organizations ranked by number of
    registered MAC prefixes.  With a query, filters organizations by
    case-insensitive substring match.

    The local OUI database is automatically initialised on first use by
    downloading IEEE CSV files (~10 seconds).

    Args:
        query: Optional substring to filter organization names
            (e.g. ``"micro"`` matches ``"Microsoft"``).  When ``None``,
            returns the top 100 by prefix count.

    Returns:
        A dictionary with ``query`` (the filter used), ``count`` (number
        of matching organizations), and ``brands`` (list of dicts each
        with ``organization``, ``country``, and ``prefix_count``).
    """
    results = list_brands(query)
    return {"query": query, "count": len(results), "brands": results}


@mcp.tool
def update_oui_database() -> dict:
    """Download fresh IEEE OUI data and rebuild the local database.

    Fetches the latest MA-L, MA-M, MA-S, IAB, and CID registries from
    IEEE and rebuilds the local SQLite database.  Takes approximately
    10 seconds depending on network speed.

    This is a write operation: it modifies the local cache at
    ``~/.cache/clr-macvendors-mcp/oui.db``.

    Returns:
        A dictionary with ``status`` (``"updated"``) and ``path``
        (filesystem path to the rebuilt database).
    """
    path = build_db()
    return {"status": "updated", "path": str(path)}


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

    read_only = args.read_only or os.environ.get("MACVENDORS_READ_ONLY", "").lower() in ("1", "true", "yes")
    if read_only and WRITE_TOOLS:
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
