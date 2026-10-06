import os
import subprocess
import sys

import pytest
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration


def test_migrations_upgrade_check_and_downgrade() -> None:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for migration tests")
    if not (make_url(url).database or "").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must point to a database ending in _test")
    environment = {**os.environ, "DATABASE_URL": url}
    for arguments in [("upgrade", "head"), ("check",), ("downgrade", "base"), ("upgrade", "head")]:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
