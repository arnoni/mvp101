from pathlib import Path

from sqlalchemy.dialects import postgresql


def test_ugc_report_insert_public_id_cast_compiles_correctly():
    routes_py = Path("app/api/routes.py").read_text(encoding="utf-8")
    assert "CAST(:public_id AS uuid)" in routes_py
    assert ":public_id::uuid" not in routes_py


def test_ugc_report_insert_sql_uses_consistent_named_bind_style():
    routes_py = Path("app/api/routes.py").read_text(encoding="utf-8")
    start = routes_py.index("UGC_INSERT_SQL = text(")
    end = routes_py.index("RETURNING id, public_id, created_at", start)
    ugc_block = routes_py[start:end]
    assert "$1" not in ugc_block
    assert "$2" not in ugc_block


def test_ugc_report_insert_sql_compiles_under_asyncpg_dialect():
    from app.api.routes import UGC_INSERT_SQL

    compiled = UGC_INSERT_SQL.compile(dialect=postgresql.asyncpg.dialect())
    sql_str = str(compiled)
    assert "CAST" in sql_str or "uuid" in sql_str
    assert ":public_id::uuid" not in sql_str
