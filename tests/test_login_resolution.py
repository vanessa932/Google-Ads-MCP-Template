"""Verifies login-customer-id is chosen per target account."""

import os
import pytest
import ads_mcp.utils as utils


class FakeCreds:
    token = "fake-token-abc"
    refresh_token = None


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    utils._accessible_cache.clear()
    utils.set_target_customer_id(None)
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "dev-token")
    # pretend the signed-in identity has direct access to these two
    monkeypatch.setattr(
        utils,
        "_directly_accessible_customers",
        lambda creds: frozenset({"1234567890", "2345678901"}),
    )
    yield
    utils._accessible_cache.clear()


def test_direct_access_sends_no_login_id(monkeypatch):
    """Manager id set, but the target account is directly accessible."""
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "1234567890")
    assert utils.resolve_login_customer_id("2345678901", FakeCreds()) is None
    assert utils.resolve_login_customer_id("123-456-7890", FakeCreds()) is None


def test_manager_mediated_still_uses_env(monkeypatch):
    """An account NOT directly accessible keeps the manager header."""
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "3456789012")
    assert (
        utils.resolve_login_customer_id("1111111111", FakeCreds())
        == "3456789012"
    )


def test_no_env_var_means_no_header(monkeypatch):
    monkeypatch.delenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", raising=False)
    assert utils.resolve_login_customer_id("1111111111", FakeCreds()) is None
    assert utils.resolve_login_customer_id("2345678901", FakeCreds()) is None


def test_no_target_falls_back_to_env(monkeypatch):
    """Non customer-scoped tools keep prior behaviour."""
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "345-678-9012")
    assert utils.resolve_login_customer_id(None, FakeCreds()) == "3456789012"


def test_client_uses_resolved_id(monkeypatch):
    """End to end through get_googleads_client via the contextvar."""
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "1234567890")
    captured = {}

    class FakeClient:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(utils, "GoogleAdsClient", FakeClient)
    monkeypatch.setattr(utils, "_create_credentials", lambda: FakeCreds())

    utils.set_target_customer_id("2345678901")
    utils.get_googleads_client()
    assert (
        "login_customer_id" not in captured
    ), "a directly accessible account must not carry the manager header"

    captured.clear()
    utils.set_target_customer_id("1111111111")
    utils.get_googleads_client()
    assert captured.get("login_customer_id") == "1234567890"

    captured.clear()
    utils.set_target_customer_id("2345678901")
    utils.get_googleads_client(login_customer_id="222-222-2222")
    assert captured.get("login_customer_id") == "2222222222", "explicit wins"


def test_several_managers_pick_the_one_holding_the_account(monkeypatch):
    """A comma-separated list routes each account through its own manager."""
    monkeypatch.setenv(
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID", "111-111-1111, 222-222-2222"
    )
    trees = {
        "1111111111": frozenset({"1111111111", "5555555555"}),
        "2222222222": frozenset({"2222222222", "6666666666"}),
    }
    monkeypatch.setattr(utils, "_manager_tree", lambda m, creds: trees[m])
    resolve = utils.resolve_login_customer_id
    assert resolve("5555555555", FakeCreds()) == "1111111111"
    assert resolve("666-666-6666", FakeCreds()) == "2222222222"
    assert resolve("2222222222", FakeCreds()) == "2222222222"
    # directly accessible accounts still send no header
    assert resolve("2345678901", FakeCreds()) is None
    # unknown account and no target fall back to the first manager
    assert resolve("9999999999", FakeCreds()) == "1111111111"
    assert resolve(None, FakeCreds()) == "1111111111"
