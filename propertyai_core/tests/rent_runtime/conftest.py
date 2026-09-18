import pytest
from .helpers import package_context


@pytest.fixture(scope="session")
def package():
    with package_context() as value:
        yield value
