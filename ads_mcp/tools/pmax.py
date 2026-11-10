"""Tool for creating Performance Max campaigns in one atomic request.

A PMax campaign has no ad groups or ads. It needs a budget, the campaign,
and an asset group holding text and image assets. Google only accepts the
campaign together with a complete asset group, so everything is sent in a
single GoogleAdsService.mutate call using temporary (negative) ids.
"""

import json
import urllib.request
from typing import Any, Dict, List

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.coordinator import mcp

_MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _as_list(value, name: str) -> List[str]:
    """Some MCP clients send arrays as JSON strings; accept both."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = [value]
        value = parsed if isinstance(parsed, list) else [parsed]
    if not isinstance(value, list):
        raise ToolError(f"'{name}' must be a list of strings.")
    return [str(v).strip() for v in value if str(v).strip()]


def _check_texts(items, name, min_n, max_n, max_len):
    if not min_n <= len(items) <= max_n:
        raise ToolError(f"Provide between {min_n} and {max_n} {name}.")
    too_long = [t for t in items if len(t) > max_len]
    if too_long:
        raise ToolError(f"{name} over {max_len} characters: {too_long}")


def _download_image(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(_MAX_IMAGE_BYTES + 1)
    except Exception as ex:
        raise ToolError(f"Could not download image {url}: {ex}")
    if len(data) > _MAX_IMAGE_BYTES:
        raise ToolError(f"Image larger than 5 MB: {url}")
    if not data:
        raise ToolError(f"Empty image: {url}")
    return data


@mcp.tool(annotations=ToolAnnotations(destructiveHint=False))
def create_performance_max_campaign(
    customer_id: str,
    name: str,
    daily_budget: float,
    final_url: str,
    business_name: str,
    headlines: List[str],
    long_headlines: List[str],
    descriptions: List[str],
    landscape_image_urls: List[str],
    square_image_urls: List[str],
    logo_urls: List[str],
    bidding_strategy: str = "MAXIMIZE_CONVERSIONS",
    target_cpa: float | None = None,
    target_roas: float | None = None,
    asset_group_name: str | None = None,
    validate_only: bool = False,
) -> Dict[str, Any]:
    """Creates a complete Performance Max campaign (budget + campaign +
    asset group + text and image assets) in PAUSED state. Run first with
    validate_only=True. Add locations afterwards with
    `add_location_targeting`.

    Args:
        customer_id: The Google Ads account.
        name: A unique campaign name.
        daily_budget: Daily budget in account currency (e.g. 50.0).
        final_url: Landing page URL.
        business_name: Business name, max 25 characters.
        headlines: 3 to 15 headlines, max 30 characters each.
        long_headlines: 1 to 5 long headlines, max 90 characters each.
        descriptions: 2 to 5 descriptions, max 90 characters each; at
            least one must be 60 characters or less.
        landscape_image_urls: 1+ public URLs of 1.91:1 images
            (recommended 1200x628, min 600x314).
        square_image_urls: 1+ public URLs of 1:1 images
            (recommended 1200x1200, min 300x300).
        logo_urls: 1 to 5 public URLs of square 1:1 logos
            (recommended 1200x1200, min 128x128).
        bidding_strategy: MAXIMIZE_CONVERSIONS or MAXIMIZE_CONVERSION_VALUE.
        target_cpa: Optional target CPA (only MAXIMIZE_CONVERSIONS).
        target_roas: Optional target ROAS, e.g. 3.5
            (only MAXIMIZE_CONVERSION_VALUE).
        asset_group_name: Optional asset group name (default: campaign
            name + " - Grupo 1").
        validate_only: If True, only validates the request.
    """
    headlines = _as_list(headlines, "headlines")
    long_headlines = _as_list(long_headlines, "long_headlines")
    descriptions = _as_list(descriptions, "descriptions")
    landscape = _as_list(landscape_image_urls, "landscape_image_urls")
    square = _as_list(square_image_urls, "square_image_urls")
    logos = _as_list(logo_urls, "logo_urls")

    _check_texts(headlines, "headlines", 3, 15, 30)
    _check_texts(long_headlines, "long_headlines", 1, 5, 90)
    _check_texts(descriptions, "descriptions", 2, 5, 90)
    if not any(len(d) <= 60 for d in descriptions):
        raise ToolError("At least one description must be 60 chars or less.")
    if not business_name or len(business_name) > 25:
        raise ToolError("business_name is required, max 25 characters.")
    if not landscape or not square:
        raise ToolError("Provide at least 1 landscape and 1 square image.")
    if len(landscape) + len(square) > 20:
        raise ToolError("Max 20 images in total (landscape + square).")
    if not 1 <= len(logos) <= 5:
        raise ToolError("Provide between 1 and 5 logos.")

    strategy = bidding_strategy.upper()
    if strategy not in ("MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"):
        raise ToolError(
            "PMax supports MAXIMIZE_CONVERSIONS or MAXIMIZE_CONVERSION_VALUE."
        )

    cid = utils.clean_customer_id(customer_id)
    utils.set_target_customer_id(cid)
    client = utils.get_googleads_client()
    ga_service = client.get_service("GoogleAdsService")
    enums = client.enums

    budget_rn = client.get_service("CampaignBudgetService").campaign_budget_path(
        cid, "-1"
    )
    campaign_rn = client.get_service("CampaignService").campaign_path(cid, "-2")
    asset_group_rn = client.get_service("AssetGroupService").asset_group_path(
        cid, "-3"
    )
    asset_service = client.get_service("AssetService")
    next_id = [-10]

    def new_asset_rn():
        rn = asset_service.asset_path(cid, str(next_id[0]))
        next_id[0] -= 1
        return rn

    budget_ops, campaign_ops, asset_ops, group_ops, link_ops = [], [], [], [], []

    # 1. Budget (PMax budgets cannot be shared).
    op = client.get_type("MutateOperation")
    budget = op.campaign_budget_operation.create
    budget.resource_name = budget_rn
    budget.name = f"{name} - Orçamento"
    budget.amount_micros = utils.micros(daily_budget)
    budget.delivery_method = enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False
    budget_ops.append(op)

    # 2. Campaign — always PAUSED.
    op = client.get_type("MutateOperation")
    campaign = op.campaign_operation.create
    campaign.resource_name = campaign_rn
    campaign.name = name
    campaign.status = enums.CampaignStatusEnum.PAUSED
    campaign.advertising_channel_type = (
        enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX
    )
    campaign.campaign_budget = budget_rn
    try:
        campaign.contains_eu_political_advertising = (
            enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )
    except AttributeError:
        pass
    # Keep business name and logo at asset-group level.
    try:
        campaign.brand_guidelines_enabled = False
    except (AttributeError, ValueError):
        pass
    if strategy == "MAXIMIZE_CONVERSIONS":
        mc = client.get_type("MaximizeConversions")
        if target_cpa:
            mc.target_cpa_micros = utils.micros(target_cpa)
        client.copy_from(campaign.maximize_conversions, mc)
    else:
        mv = client.get_type("MaximizeConversionValue")
        if target_roas:
            mv.target_roas = float(target_roas)
        client.copy_from(campaign.maximize_conversion_value, mv)
    campaign_ops.append(op)

    # 3. Asset group.
    op = client.get_type("MutateOperation")
    group = op.asset_group_operation.create
    group.resource_name = asset_group_rn
    group.name = asset_group_name or f"{name} - Grupo 1"
    group.campaign = campaign_rn
    group.final_urls.append(final_url)
    group.final_mobile_urls.append(final_url)
    group.status = enums.AssetGroupStatusEnum.ENABLED
    group_ops.append(op)

    def link(asset_rn, field_type):
        op = client.get_type("MutateOperation")
        aga = op.asset_group_asset_operation.create
        aga.asset = asset_rn
        aga.asset_group = asset_group_rn
        aga.field_type = field_type
        link_ops.append(op)

    def add_text(text, field_type):
        rn = new_asset_rn()
        op = client.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = rn
        asset.text_asset.text = text
        asset_ops.append(op)
        link(rn, field_type)

    def add_image(url, field_type, label):
        rn = new_asset_rn()
        op = client.get_type("MutateOperation")
        asset = op.asset_operation.create
        asset.resource_name = rn
        asset.name = f"{name} - {label} {abs(next_id[0])}"
        asset.image_asset.data = _download_image(url)
        asset_ops.append(op)
        link(rn, field_type)

    ft = enums.AssetFieldTypeEnum
    for t in headlines:
        add_text(t, ft.HEADLINE)
    for t in long_headlines:
        add_text(t, ft.LONG_HEADLINE)
    for t in descriptions:
        add_text(t, ft.DESCRIPTION)
    add_text(business_name, ft.BUSINESS_NAME)
    for u in landscape:
        add_image(u, ft.MARKETING_IMAGE, "Paisagem")
    for u in square:
        add_image(u, ft.SQUARE_MARKETING_IMAGE, "Quadrada")
    for u in logos:
        add_image(u, ft.LOGO, "Logo")

    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = cid
    request.validate_only = validate_only
    for op in budget_ops + campaign_ops + asset_ops + group_ops + link_ops:
        request.mutate_operations.append(op)

    try:
        response = ga_service.mutate(request=request)
    except GoogleAdsException as ex:
        raise utils.to_tool_error(ex)

    if validate_only:
        return {
            "validate_only": True,
            "message": "PMax request is valid. No changes were applied.",
            "assets": len(asset_ops),
        }

    created_campaign = None
    for r in response.mutate_operation_responses:
        rn = r.campaign_result.resource_name
        if rn:
            created_campaign = rn
            break

    # Read back the real state instead of trusting the mutate response.
    state = []
    if created_campaign:
        campaign_id = created_campaign.split("/")[-1]
        state = utils.run_query(
            cid,
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.advertising_channel_type, asset_group.id, "
            "asset_group.name, asset_group.status FROM asset_group "
            f"WHERE campaign.id = {campaign_id}",
        )
    return {
        "validate_only": False,
        "campaign": created_campaign,
        "assets_created": len(asset_ops),
        "read_back": state,
        "next_step": "Add locations with add_location_targeting. "
        "Campaign stays PAUSED until enabled with set_campaign_status.",
    }
