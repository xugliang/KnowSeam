import pytest
from knowseam.config import CollectionConfig, EmployeeConfig


def test_public_default_and_restricted_default_deny():
    assert CollectionConfig(id="public", title="全员").visibility == "company"
    for mode in ("department", "employee"):
        item = CollectionConfig(id="restricted", title="受限", visibility=mode)
        assert not item.departments and not item.employees


def test_strict_policy_does_not_silently_ignore_wrong_grants():
    with pytest.raises(ValueError):
        CollectionConfig(id="public", title="全员", employees=["employee-a"])
    with pytest.raises(ValueError):
        CollectionConfig(id="restricted", title="部门", visibility="department", employees=["employee-a"])
    with pytest.raises(ValueError):
        EmployeeConfig(actor_id="employee-a", active="true")
