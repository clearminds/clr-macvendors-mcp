"""OUI database module for IEEE prefix data.

Downloads IEEE CSV files (MA-L, MA-M, MA-S, IAB, CID), builds a local
SQLite database, and provides brand-to-prefix lookup queries.
"""

import csv
import io
import logging
import os
import sqlite3
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

DB_DIR = Path.home() / ".cache" / "clr-macvendors-mcp"
DB_PATH = DB_DIR / "oui.db"

IEEE_CSV_URLS = [
    # MA-L (MAC Address Block Large — classic OUI, 24-bit)
    "https://standards-oui.ieee.org/oui/oui.csv",
    # MA-M (MAC Address Block Medium, 28-bit)
    "https://standards-oui.ieee.org/oui28/mam.csv",
    # MA-S (MAC Address Block Small, 36-bit)
    "https://standards-oui.ieee.org/oui36/oui36.csv",
    # IAB (Individual Address Block — legacy, 36-bit)
    "https://standards-oui.ieee.org/iab/iab.csv",
    # CID (Company ID — not used for network addresses)
    "https://standards-oui.ieee.org/cid/cid.csv",
]

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS oui (
    prefix TEXT PRIMARY KEY,
    registry TEXT NOT NULL,
    organization TEXT NOT NULL,
    address TEXT,
    country TEXT
)
"""

_CREATE_INDEX = """\
CREATE INDEX IF NOT EXISTS idx_org ON oui(organization COLLATE NOCASE)
"""


def _normalize_prefix(assignment: str) -> str:
    """Convert an IEEE hex assignment to colon-separated format.

    Pairs hex characters from left to right, producing colon-separated
    octets (or nibble groups for shorter assignments).

    Args:
        assignment: Raw hex string from the IEEE CSV (e.g. ``"286FB9"``,
            ``"C85CE27"``).

    Returns:
        Colon-separated prefix string (e.g. ``"28:6F:B9"``,
        ``"C8:5C:E2:7"``).
    """
    raw = assignment.strip().upper()
    # Pair hex chars from the left; a trailing odd char stays as-is
    parts: list[str] = []
    i = 0
    while i < len(raw):
        if i + 2 <= len(raw):
            parts.append(raw[i : i + 2])
            i += 2
        else:
            parts.append(raw[i:])
            i += 1
    return ":".join(parts)


def _extract_country(address: str) -> str | None:
    """Extract a 2-letter ISO country code from an IEEE address field.

    The IEEE CSV address field is a single string with comma-separated
    parts.  The country code is typically the last all-alpha token when
    the address is split on whitespace/commas, or the second-to-last
    token when a postal code follows.

    Args:
        address: Raw address string from the IEEE CSV.

    Returns:
        A 2-letter uppercase country code, or ``None`` if one cannot be
        determined.
    """
    if not address:
        return None
    # Split on whitespace and commas, strip empties
    tokens = [t.strip() for t in address.replace(",", " ").split() if t.strip()]
    if not tokens:
        return None
    # Walk backwards looking for a 2-letter alpha token
    for token in reversed(tokens):
        if len(token) == 2 and token.isalpha():
            return token.upper()
    return None


def build_db() -> Path:
    """Download all IEEE CSV files and build the local SQLite OUI database.

    Downloads MA-L, MA-M, MA-S, IAB, and CID registries from IEEE,
    parses the CSV data, and inserts rows into a SQLite database at
    :data:`DB_PATH`.  Uses atomic replace (write to ``.tmp`` then rename)
    so concurrent readers never see a partial database.

    Returns:
        Path to the newly built database file.

    Raises:
        httpx.HTTPStatusError: If any IEEE download fails.
    """
    DB_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = DB_PATH.with_suffix(".tmp")

    # Remove stale tmp if it exists
    if tmp_path.exists():
        tmp_path.unlink()

    conn = sqlite3.connect(str(tmp_path))
    try:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_INDEX)
        conn.execute("PRAGMA journal_mode=WAL")

        total = 0
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            for url in IEEE_CSV_URLS:
                logger.info("Downloading %s", url)
                resp = client.get(url)
                resp.raise_for_status()

                reader = csv.DictReader(io.StringIO(resp.text))
                rows: list[tuple[str, str, str, str | None, str | None]] = []
                for row in reader:
                    assignment = row.get("Assignment", "").strip()
                    if not assignment:
                        continue
                    prefix = _normalize_prefix(assignment)
                    registry = row.get("Registry", "").strip()
                    org = row.get("Organization Name", "").strip()
                    address = row.get("Organization Address", "").strip()
                    country = _extract_country(address)
                    rows.append((prefix, registry, org, address or None, country))

                conn.executemany(
                    "INSERT OR REPLACE INTO oui (prefix, registry, organization, address, country) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                logger.info("Loaded %d entries from %s", len(rows), url)
                total += len(rows)

        conn.commit()
        logger.info("Total OUI entries: %d", total)
    finally:
        conn.close()

    # Atomic replace
    os.replace(str(tmp_path), str(DB_PATH))
    logger.info("OUI database written to %s", DB_PATH)
    return DB_PATH


def _ensure_db() -> Path:
    """Ensure the OUI database exists, building it if necessary.

    Returns:
        Path to the database file.
    """
    if not DB_PATH.exists():
        logger.info("OUI database not found, building...")
        build_db()
    return DB_PATH


def search_by_brand(brand: str) -> list[dict]:
    """Search the OUI table for prefixes belonging to a brand/organization.

    Performs a case-insensitive ``LIKE %%brand%%`` search against the
    organization column.

    Args:
        brand: Organization name or substring to search for
            (e.g. ``"Cisco"``, ``"Nokia"``).

    Returns:
        A list of dicts, each with keys ``prefix``, ``registry``,
        ``organization``, and ``country``.
    """
    db_path = _ensure_db()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            "SELECT prefix, registry, organization, country "
            "FROM oui WHERE organization LIKE ? COLLATE NOCASE "
            "ORDER BY organization, prefix",
            (f"%{brand}%",),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def list_brands(query: str | None = None) -> list[dict]:
    """List organizations in the OUI database, grouped with prefix counts.

    Args:
        query: Optional substring filter (case-insensitive ``LIKE``).
            If ``None``, returns the top 100 organizations by prefix
            count.

    Returns:
        A list of dicts, each with keys ``organization``, ``country``,
        and ``prefix_count``.
    """
    db_path = _ensure_db()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if query:
            cursor = conn.execute(
                "SELECT organization, country, COUNT(*) AS prefix_count "
                "FROM oui WHERE organization LIKE ? COLLATE NOCASE "
                "GROUP BY organization "
                "ORDER BY prefix_count DESC",
                (f"%{query}%",),
            )
        else:
            cursor = conn.execute(
                "SELECT organization, country, COUNT(*) AS prefix_count "
                "FROM oui "
                "GROUP BY organization "
                "ORDER BY prefix_count DESC "
                "LIMIT 100",
            )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
