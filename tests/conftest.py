"""Shared fixtures.

Settings live here rather than in each test module so that adding a config
field does not require touching every test file — which it did, twice.
"""

from __future__ import annotations

import pytest

from otk.config import Settings
from otk.service import Store


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        secret="test-pepper",
        env="test",
        max_file_bytes=1024 * 1024,
        max_ticket_bytes=4 * 1024 * 1024,
        max_body_bytes=6 * 1024 * 1024,
        max_files_per_ticket=5,
        # Off by default so ordinary tests are not throttled; the rate-limit
        # tests build their own Settings with a real cap.
        rate_limit_per_minute=0,
    )


@pytest.fixture
def store(settings) -> Store:
    return Store(settings)
