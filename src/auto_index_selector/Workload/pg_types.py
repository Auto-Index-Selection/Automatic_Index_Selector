"""
PostgreSQL Type OID to Type Name resolver and value converter.

Provides built-in mapping of standard PostgreSQL data types and optional
dynamic resolution for user-defined or extension types via psql.
"""

import json
import logging
import subprocess
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Standard PostgreSQL built-in type OIDs mapped to their canonical typname
# Source: PostgreSQL 16 catalog (pg_type)
PG_BUILTIN_TYPES: Dict[int, str] = {
    16: "bool",
    17: "bytea",
    18: "char",
    19: "name",
    20: "int8",
    21: "int2",
    22: "int2vector",
    23: "int4",
    24: "regproc",
    25: "text",
    26: "oid",
    27: "tid",
    28: "xid",
    29: "cid",
    30: "oidvector",
    32: "pg_ddl_command",
    71: "pg_type",
    75: "pg_attribute",
    81: "pg_proc",
    83: "pg_class",
    114: "json",
    142: "xml",
    143: "_xml",
    194: "pg_node_tree",
    199: "_json",
    210: "_pg_type",
    269: "table_am_handler",
    270: "_pg_attribute",
    271: "_xid8",
    272: "_pg_proc",
    273: "_pg_class",
    325: "index_am_handler",
    600: "point",
    601: "lseg",
    602: "path",
    603: "box",
    604: "polygon",
    628: "line",
    629: "_line",
    650: "cidr",
    651: "_cidr",
    700: "float4",
    701: "float8",
    705: "unknown",
    718: "circle",
    719: "_circle",
    774: "macaddr8",
    775: "_macaddr8",
    790: "money",
    791: "_money",
    829: "macaddr",
    869: "inet",
    1000: "_bool",
    1001: "_bytea",
    1002: "_char",
    1003: "_name",
    1005: "_int2",
    1006: "_int2vector",
    1007: "_int4",
    1008: "_regproc",
    1009: "_text",
    1010: "_tid",
    1011: "_xid",
    1012: "_cid",
    1013: "_oidvector",
    1014: "_bpchar",
    1015: "_varchar",
    1016: "_int8",
    1017: "_point",
    1018: "_lseg",
    1019: "_path",
    1020: "_box",
    1021: "_float4",
    1022: "_float8",
    1027: "_polygon",
    1028: "_oid",
    1033: "aclitem",
    1034: "_aclitem",
    1040: "_macaddr",
    1041: "_inet",
    1042: "bpchar",
    1043: "varchar",
    1082: "date",
    1083: "time",
    1114: "timestamp",
    1115: "_timestamp",
    1182: "_date",
    1183: "_time",
    1184: "timestamptz",
    1185: "_timestamptz",
    1186: "interval",
    1187: "_interval",
    1231: "_numeric",
    1248: "pg_database",
    1263: "_cstring",
    1266: "timetz",
    1270: "_timetz",
    1560: "bit",
    1561: "_bit",
    1562: "varbit",
    1563: "_varbit",
    1700: "numeric",
    1790: "refcursor",
    2201: "_refcursor",
    2202: "regprocedure",
    2203: "regoper",
    2204: "regoperator",
    2205: "regclass",
    2206: "regtype",
    2207: "_regprocedure",
    2208: "_regoper",
    2209: "_regoperator",
    2210: "_regclass",
    2211: "_regtype",
    2249: "record",
    2275: "cstring",
    2276: "any",
    2277: "anyarray",
    2278: "void",
    2279: "trigger",
    2280: "language_handler",
    2281: "internal",
    2283: "anyelement",
    2287: "_record",
    2776: "anynonarray",
    2842: "pg_authid",
    2843: "pg_auth_members",
    2949: "_txid_snapshot",
    2950: "uuid",
    2951: "_uuid",
    2970: "txid_snapshot",
    3115: "fdw_handler",
    3220: "pg_lsn",
    3221: "_pg_lsn",
    3310: "tsm_handler",
    3361: "pg_ndistinct",
    3402: "pg_dependencies",
    3500: "anyenum",
    3614: "tsvector",
    3615: "tsquery",
    3642: "gtsvector",
    3643: "_tsvector",
    3644: "_gtsvector",
    3645: "_tsquery",
    3734: "regconfig",
    3735: "_regconfig",
    3769: "regdictionary",
    3770: "_regdictionary",
    3802: "jsonb",
    3807: "_jsonb",
    3831: "anyrange",
    3838: "event_trigger",
    3904: "int4range",
    3905: "_int4range",
    3906: "numrange",
    3907: "_numrange",
    3908: "tsrange",
    3909: "_tsrange",
    3910: "tstzrange",
    3911: "_tstzrange",
    3912: "daterange",
    3913: "_daterange",
    3926: "int8range",
    3927: "_int8range",
    4066: "pg_shseclabel",
    4072: "jsonpath",
    4073: "_jsonpath",
    4089: "regnamespace",
    4090: "_regnamespace",
    4096: "regrole",
    4097: "_regrole",
    4191: "regcollation",
    4192: "_regcollation",
    4451: "int4multirange",
    4532: "nummultirange",
    4533: "tsmultirange",
    4534: "tstzmultirange",
    4535: "datemultirange",
    4536: "int8multirange",
    4537: "anymultirange",
    4538: "anycompatiblemultirange",
    4600: "pg_brin_bloom_summary",
    4601: "pg_brin_minmax_multi_summary",
    5017: "pg_mcv_list",
    5038: "pg_snapshot",
    5039: "_pg_snapshot",
    5069: "xid8",
    5077: "anycompatible",
    5078: "anycompatiblearray",
    5079: "anycompatiblenonarray",
    5080: "anycompatiblerange",
    6101: "pg_subscription",
    6150: "_int4multirange",
    6151: "_nummultirange",
    6152: "_tsmultirange",
    6153: "_tstzmultirange",
    6155: "_datemultirange",
    6157: "_int8multirange",
}

# In-memory cache for dynamic resolutions
_DYNAMIC_TYPE_CACHE: Dict[int, str] = {}


def resolve_type_oid(
    oid: int,
    dbname: Optional[str] = None,
    allow_dynamic_lookup: bool = True
) -> str:
    """
    Resolves a PostgreSQL type OID to its type name.

    1. Checks the static built-in mapping.
    2. Checks the dynamic resolution cache.
    3. If not found and allow_dynamic_lookup is True, queries pg_type via psql.
    4. Falls back to 'unknown_<oid>' if unresolvable.
    """
    if oid in PG_BUILTIN_TYPES:
        return PG_BUILTIN_TYPES[oid]

    if oid in _DYNAMIC_TYPE_CACHE:
        return _DYNAMIC_TYPE_CACHE[oid]

    if allow_dynamic_lookup:
        resolved = _query_pg_type(oid, dbname=dbname)
        if resolved:
            _DYNAMIC_TYPE_CACHE[oid] = resolved
            return resolved

    # Fallback when OID cannot be resolved
    logger.warning(f"Unrecognized PostgreSQL type OID {oid}; fallback to unknown_{oid}")
    fallback = f"unknown_{oid}"
    _DYNAMIC_TYPE_CACHE[oid] = fallback
    return fallback


def _query_pg_type(oid: int, dbname: Optional[str] = None) -> Optional[str]:
    """
    Attempts to look up an OID in PostgreSQL's pg_type catalog via psql CLI.
    """
    cmd = ["psql", "-t", "-A", "-c", f"SELECT typname FROM pg_type WHERE oid = {int(oid)};"]
    if dbname:
        cmd.extend(["-d", dbname])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        if res.returncode == 0:
            typname = res.stdout.strip()
            if typname:
                return typname
    except Exception as e:
        logger.debug(f"Dynamic lookup failed for OID {oid}: {e}")
    return None


# Integer type names
INTEGER_TYPES = {
    "int2", "int4", "int8", "oid", "cid", "xid", "xid8", "tid"
}

# Floating / Numeric type names
FLOAT_TYPES = {
    "float4", "float8"
}

NUMERIC_TYPES = {
    "numeric"
}

# Boolean types
BOOLEAN_TYPES = {
    "bool"
}


def convert_param_value(type_name: str, val_str: Any, is_null: bool = False) -> Any:
    """
    Converts raw parameter string from logger to appropriate JSON value based on PostgreSQL type.

    - If is_null is True or val_str is None -> returns None (JSON null)
    - int2, int4, int8, oid -> int
    - float4, float8 -> float
    - numeric -> int (if no fractional part) or float
    - bool -> bool (True/False)
    - strings / dates / others -> str
    """
    if is_null or val_str is None:
        return None

    if not isinstance(val_str, str):
        # Already a non-string type (e.g. if loaded directly)
        return val_str

    # Integer conversion
    if type_name in INTEGER_TYPES:
        try:
            return int(val_str)
        except ValueError:
            return val_str

    # Float conversion
    if type_name in FLOAT_TYPES:
        try:
            return float(val_str)
        except ValueError:
            return val_str

    # Numeric conversion
    if type_name in NUMERIC_TYPES:
        try:
            if "." in val_str or "e" in val_str.lower():
                return float(val_str)
            return int(val_str)
        except ValueError:
            return val_str

    # Boolean conversion
    if type_name in BOOLEAN_TYPES:
        low = val_str.strip().lower()
        if low in ("t", "true", "1", "y", "yes"):
            return True
        if low in ("f", "false", "0", "n", "no"):
            return False
        return val_str

    # Strings (bpchar, varchar, text, etc.), dates, timestamps, uuids, etc.
    return val_str
