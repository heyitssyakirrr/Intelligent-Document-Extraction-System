"""Minimal DB2 LUW writer for ELDS.OCR."""

from __future__ import annotations

import os
import subprocess
from typing import Sequence

import ibm_db
import ibm_db_dbi

TABLE = '"ELDS"."OCR"'
INSERT_SQL = (
    f"INSERT INTO {TABLE} "
    '("FILENAME", "BANKNAME", "FICODE", "MASTERACCNO", "SUBACCNO") '
    "VALUES (?, ?, ?, ?, ?)"
)


def connect_db2(
    database: str,
    hostname: str,
    port: int | str = 50000,
    uid: str = "",
    pwd: str | None = None,
    autocommit: bool = True,
):
    """Open a DB2 LUW connection and return a DB2 connection object."""
    if pwd is None:
        pwd = os.getenv("DB2_PWD", "")
    if not database or not hostname or not uid:
        raise ValueError("database, hostname, and uid are required")

    dsn = (
        f"DATABASE={database};"
        f"HOSTNAME={hostname};"
        f"PORT={port};"
        "PROTOCOL=TCPIP;"
        f"UID={uid};"
        f"PWD={pwd};"
    )

    conn = ibm_db.connect(dsn, "", "")
    dbi_conn = ibm_db_dbi.Connection(conn)
    dbi_conn.set_autocommit(autocommit)
    return dbi_conn


def kinit_from_keytab(keytab: str, principal: str, timeout: float = 30.0) -> None:
    """Obtain/refresh a Kerberos ticket for the gMSA principal from its keytab."""
    result = subprocess.run(
        ["kinit", "-k", "-t", keytab, principal],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"kinit failed for '{principal}' using '{keytab}' "
            f"(exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        )


def connect_db2_gmsa(
    database: str,
    hostname: str,
    port: int | str = 50000,
    autocommit: bool = True,
):
    """Open a DB2 LUW connection using the gMSA's Kerberos ticket (no UID/PWD)."""
    if not database or not hostname:
        raise ValueError("database and hostname are required")

    dsn = (
        f"DATABASE={database};"
        f"HOSTNAME={hostname};"
        f"PORT={port};"
        "PROTOCOL=TCPIP;"
        "AUTHENTICATION=KERBEROS;"
    )

    conn = ibm_db.connect(dsn, "", "")
    dbi_conn = ibm_db_dbi.Connection(conn)
    dbi_conn.set_autocommit(autocommit)
    return dbi_conn


def append_filereader_row(
    conn,
    filename: str,
    bankname: str | None = None,
    ficode: str | None = None,
    masteraccno: str | None = None,
    subaccno: str | None = None,
) -> None:
    """Append one row into ELDS.OCR. TIMESTAMP uses the table default."""
    TABLE = '"ELDS"."OCR"'
    INSERT_SQL = (
        f"INSERT INTO {TABLE} "
        '("FILENAME", "BANKNAME", "FICODE", "MASTERACCNO", "SUBACCNO") '
        "VALUES (?, ?, ?, ?, ?)"
    )
    cursor = conn.cursor()
    try:
        cursor.execute(
            INSERT_SQL,
            (filename, bankname, ficode, masteraccno, subaccno),
        )
    finally:
        cursor.close()
