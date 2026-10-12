"""
One-time (idempotent) database bootstrap, run by a superuser:

* creates the two login roles (no superuser, NO BYPASSRLS),
* creates/owns the database,
* sets default privileges so the app role can SELECT/INSERT/UPDATE tables the owner
  creates but can NEVER DELETE or TRUNCATE (business data is never hard-deleted).

The Django app itself never uses the superuser; only `manage.py bootstrap_db` and the
test suite do.
"""
import psycopg
from psycopg import sql


def _connect(host, port, user, password, dbname):
    return psycopg.connect(host=host, port=port, user=user, password=password or None, dbname=dbname, autocommit=True)


def _ensure_role(cur, name, password, flags):
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [name])
    verb = "ALTER" if cur.fetchone() else "CREATE"
    cur.execute(
        sql.SQL("{} ROLE {} LOGIN PASSWORD {} {}").format(
            sql.SQL(verb), sql.Identifier(name), sql.Literal(password), sql.SQL(flags)
        )
    )


def bootstrap(*, host, port, admin_user, admin_password, db_name, owner, owner_password, app_user, app_password, recreate=False):
    role_flags = "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"

    with _connect(host, port, admin_user, admin_password, "postgres") as conn:
        cur = conn.cursor()
        _ensure_role(cur, owner, owner_password, role_flags)
        _ensure_role(cur, app_user, app_password, role_flags)
        if recreate:
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db_name)))
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", [db_name])
        if cur.fetchone():
            cur.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(sql.Identifier(db_name), sql.Identifier(owner)))
        else:
            cur.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(db_name), sql.Identifier(owner)))

    db, o, a = sql.Identifier(db_name), sql.Identifier(owner), sql.Identifier(app_user)
    with _connect(host, port, admin_user, admin_password, db_name) as conn:
        cur = conn.cursor()
        for stmt in [
            "REVOKE ALL ON DATABASE {db} FROM PUBLIC",
            "GRANT CONNECT ON DATABASE {db} TO {o}, {a}",
            "ALTER SCHEMA public OWNER TO {o}",
            "REVOKE ALL ON SCHEMA public FROM PUBLIC",
            "GRANT USAGE ON SCHEMA public TO {a}",
            # Tables created by the owner: app role may read/insert/update, never delete/truncate.
            "ALTER DEFAULT PRIVILEGES FOR ROLE {o} IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO {a}",
            "ALTER DEFAULT PRIVILEGES FOR ROLE {o} IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {a}",
        ]:
            cur.execute(sql.SQL(stmt).format(db=db, o=o, a=a))
