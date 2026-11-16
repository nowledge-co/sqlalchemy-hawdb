import pytest
from sqlalchemy.engine import URL


@pytest.fixture(params=[False, True], ids=["file", "memory"])
def database_url(request, tmp_path):
    if request.param:
        return "hawdb:///:memory:"
    return URL.create("hawdb", database=str(tmp_path / "results.hawdb"))


@pytest.fixture(params=[False, True], ids=["transaction", "autocommit"])
def autocommit(request):
    return request.param
