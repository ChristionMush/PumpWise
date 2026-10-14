"""The same image runs in CI and on the single-server deploy kit (deploy/), which passes settings its own way."""
from rest_framework.test import APIClient

from config.settings import db_from_url


def test_database_urls_from_the_deploy_kit_are_understood():
    d = db_from_url("postgres://pumpwise_app:p%40ss%2Fword@db:5432/pumpwise", atomic=True)
    assert (d["USER"], d["PASSWORD"], d["HOST"], d["PORT"], d["NAME"], d["ATOMIC_REQUESTS"]) == ("pumpwise_app", "p@ss/word", "db", "5432", "pumpwise", True)
    assert db_from_url("postgresql://u:x@h/n", atomic=False)["PORT"] == "5432"


def test_health_answers_with_and_without_the_slash():
    for path in ("/health", "/health/"):
        res = APIClient().get(path)
        assert res.status_code == 200 and res.json()["status"] == "ok" and "version" in res.json()
