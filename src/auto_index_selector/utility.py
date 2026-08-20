from pathlib import Path
from pyprojroot import here

DEFAULT_CREATE_INDEX_PATH = Path(str(here() / "indexes" / "create_index.sql"))
DEFAULT_DELETE_INDEX_PATH = Path(str(here() / "indexes" / "delete_index.sql"))

def index_name(table: str, columns) -> str:
    """
    Build a Postgres-style default index name: <table>_<col1>_<col2>..._idx
 
    This mirrors the name Postgres itself assigns to an unnamed
    `CREATE INDEX ON table(col1, col2)` statement, so the same name can
    be used later in a matching `DROP INDEX <name>;` statement.
    """
    return f"{table}_{'_'.join(columns)}_idx"


def generate_create_index_sql(
    config, output_path: Path = DEFAULT_CREATE_INDEX_PATH
) -> Path:
    """
    Write one unnamed `create index on table(col1,col2);` statement per
    index in `config` (Postgres assigns the default name itself).
 
    `config` is a frozenset (or any iterable) of (table, (col1, col2, ...))
    tuples, e.g. the output of a ConfigSelection module such as
    `cs_module.greedyMK(...)`.
    """
    lines = [
        f"create index on {table}({','.join(columns)});"
        for table, columns in sorted(config)
    ]
    output_path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return output_path
 
 
def generate_delete_index_sql(
    config, output_path: Path = DEFAULT_DELETE_INDEX_PATH
) -> Path:
    """
    Write one `drop index <name>;` statement per index in `config`,
    computing each index's default Postgres name via index_name() so
    this file undoes exactly what generate_create_index_sql() created.
 
    `config` is a frozenset (or any iterable) of (table, (col1, col2, ...))
    tuples, e.g. the output of a ConfigSelection module such as
    `cs_module.greedyMK(...)`.
    """
    lines = [
        f"drop index {index_name(table, columns)};"
        for table, columns in sorted(config)
    ]
    output_path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return output_path