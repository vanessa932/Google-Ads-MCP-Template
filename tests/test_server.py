"""Smoke tests: the server imports and registers the expected tools."""

import asyncio

from ads_mcp.server import mcp

EXPECTED_TOOLS = {
    # read / monitoring
    "list_accessible_customers",
    "get_account_hierarchy",
    "search",
    "get_resource_metadata",
    "get_campaign_performance",
    "get_ad_group_performance",
    "get_keyword_performance",
    "get_search_terms_report",
    "get_change_history",
    "list_conversion_actions",
    "list_campaign_criteria",
    "suggest_geo_targets",
    # write / management
    "set_conversion_action_primary",
    "set_campaign_conversion_goals",
    "add_location_targeting",
    "remove_campaign_criterion",
    "set_ad_schedule",
    "create_call_asset",
    "link_asset_to_campaign",
    "set_campaign_target_cpa",
    "create_campaign_budget",
    "update_campaign_budget",
    "create_campaign",
    "set_campaign_status",
    "create_ad_group",
    "update_ad_group",
    "create_responsive_search_ad",
    "set_ad_status",
    "add_keywords",
    "set_keyword_status",
    "add_negative_keywords",
    "create_performance_max_campaign",
}


def _tools_by_name():
    tools = asyncio.run(mcp.list_tools())
    return {t.name: t for t in tools}


def test_all_tools_registered():
    registered = set(_tools_by_name())
    missing = EXPECTED_TOOLS - registered
    assert not missing, f"Missing tools: {missing}"


def test_search_tool_lists_resources():
    search_tool = _tools_by_name()["search"]
    assert "campaign" in search_tool.description
    assert "Valid resources" in search_tool.description
