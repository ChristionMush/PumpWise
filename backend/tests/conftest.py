"""
Test harness for a real RLS setup.

* Session start: a fresh `fuelstation_test` database is created with the SAME two-role model
  as production (bootstrap) and migrated as `app_owner`.
* Tests run as `app_user` (non-superuser, no BYPASSRLS, no DELETE grant) in autocommit mode,
  so SET LOCAL / transactions behave exactly as in production.
* After each test every table is emptied through the owner connection.
"""
import pytest
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.db import connections

from apps.core.bootstrap import bootstrap


@pytest.fixture(scope="session")
def django_db_setup(django_db_blocker):
    default, owner = settings.DATABASES["default"], settings.DATABASES["owner"]
    with django_db_blocker.unblock():
        bootstrap(
            host=default["HOST"],
            port=default["PORT"],
            admin_user=settings.DB_ADMIN_USER,
            admin_password=settings.DB_ADMIN_PASSWORD,
            db_name=default["NAME"],
            owner=owner["USER"],
            owner_password=owner["PASSWORD"],
            app_user=default["USER"],
            app_password=default["PASSWORD"],
            recreate=True,
        )
        call_command("migrate", database="owner", verbosity=0, interactive=False)
    yield
    for alias in ("default", "owner"):
        connections[alias].close()


@pytest.fixture(autouse=True)
def _database(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        cache.clear()
        yield
        connections["default"].close()
        with connections["owner"].cursor() as cursor:
            cursor.execute(
                "SELECT quote_ident(tablename) FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'django_migrations'"
            )
            tables = [row[0] for row in cursor.fetchall()]
            cursor.execute("TRUNCATE " + ", ".join(tables) + " CASCADE")
        connections["owner"].close()
