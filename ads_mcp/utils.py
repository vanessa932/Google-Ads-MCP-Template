"""Shared helpers: Google Ads client construction, auth, output formatting.

Credential resolution order for Google Ads API calls:

1. The OAuth access token of the user connected to this MCP server
   (present when the server runs remotely behind the FastMCP Google
   OAuth provider — e.g. a claude.ai custom connector).
2. A static OAuth refresh token from the environment
   (GOOGLE_ADS_REFRESH_TOKEN + GOOGLE_ADS_OAUTH_CLIENT_ID +
   GOOGLE_ADS_OAUTH_CLIENT_SECRET).
3. Google Application Default Credentials with the AdWords scope.
"""

import contextvars
import logging
import os
import threading
from typing import Any, List, Optional

import google.auth
import proto
from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.util import get_nested_attr
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message as PbMessage

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
_TOKEN_URI = "https://oauth2.googleapis.com/token"

# filename for the bundled list of GAQL resources used by the search tool
_GAQL_FILENAME = "gaql_resources.txt"

# Customer id the current tool call targets. Set by the LoginCustomerMiddleware
# so client construction can pick the right login-customer-id without every
# tool having to thread the value through.
_target_customer_id: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("target_customer_id", default=None)
)

# Cache of directly-accessible customer ids, keyed by credential fingerprint —
# in remote OAuth mode each connected user has their own set.
_accessible_cache: dict = {}
_accessible_lock = threading.Lock()


def _get_fastmcp_access_token():
    """Returns the connected user's OAuth token, if the server is running
    behind the FastMCP OAuth provider; None otherwise."""
    try:
        from fastmcp.server.dependencies import get_access_token

        return get_access_token()
    except Exception:
        return None


def _create_credentials() -> google.auth.credentials.Credentials:
    from google.oauth2.credentials import Credentials

    token_obj = _get_fastmcp_access_token()
    if token_obj and token_obj.token:
        return Credentials(token=token_obj.token)

    refresh_token = os.environ.get("GOOGLE_ADS_REFRESH_TOKEN")
    client_id = os.environ.get("GOOGLE_ADS_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_ADS_OAUTH_CLIENT_SECRET")
    if refresh_token and client_id and client_secret:
        return Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=_TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=[ADS_SCOPE],
        )

    credentials, _ = google.auth.default(scopes=[ADS_SCOPE])
    return credentials


def _get_developer_token() -> Optional[str]:
    # Since September 2026 Google Ads API access is tied to the Google Cloud
    # project and the developer token is optional (ignored by the API), so
    # it is only sent when one is configured.
    return os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN") or None


def set_target_customer_id(customer_id: Optional[str]) -> None:
    """Records which account the in-flight tool call is addressing."""
    _target_customer_id.set(
        clean_customer_id(customer_id) if customer_id else None
    )


def _credential_key(credentials) -> str:
    """Stable per-identity cache key that never holds a whole token."""
    secret = (
        getattr(credentials, "token", None)
        or getattr(credentials, "refresh_token", None)
        or "adc"
    )
    return str(hash(secret))


def _directly_accessible_customers(credentials) -> frozenset:
    """Customer ids the authenticated identity can reach without a manager.

    Queried with no login-customer-id, which is what makes it able to answer
    the question at all. Cached per identity — the set only changes when
    someone is granted or removed from an account.
    """
    key = _credential_key(credentials)
    with _accessible_lock:
        if key in _accessible_cache:
            return _accessible_cache[key]
    try:
        probe = GoogleAdsClient(
            credentials=credentials,
            developer_token=_get_developer_token(),
            use_proto_plus=True,
        )
        response = probe.get_service(
            "CustomerService"
        ).list_accessible_customers()
        result = frozenset(
            rn.removeprefix("customers/") for rn in response.resource_names
        )
    except Exception as ex:  # non-fatal: fall back to env-var behaviour
        logger.warning("ads_mcp: could not list accessible customers: %s", ex)
        result = frozenset()
    with _accessible_lock:
        _accessible_cache[key] = result
    return result


def resolve_login_customer_id(
    customer_id: Optional[str], credentials
) -> Optional[str]:
    """Picks the login-customer-id header value for a given target account.

    Google requires login-customer-id only when reaching a client account
    *through* a manager. Sending a manager id for an account the user can
    already access directly is not merely redundant — Google rejects the call
    with "User doesn't have permission to access customer". So:

      - target is directly accessible -> send nothing
      - otherwise                     -> send the configured manager id

    That keeps manager-mediated setups working while letting a single
    deployment also address standalone accounts the user owns outright.
    """
    managers = _configured_managers()
    if not customer_id:
        return managers[0] if managers else None

    target = clean_customer_id(customer_id)
    if target in _directly_accessible_customers(credentials):
        return None
    if len(managers) <= 1:
        return managers[0] if managers else None

    # Several managers configured: use the one whose tree holds the target.
    for manager in managers:
        if target == manager or target in _manager_tree(manager, credentials):
            return manager
    return managers[0]


def _configured_managers() -> List[str]:
    """GOOGLE_ADS_LOGIN_CUSTOMER_ID as a list; accepts several manager ids
    separated by commas (e.g. one per agency squad)."""
    raw = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or ""
    return [clean_customer_id(m) for m in raw.split(",") if m.strip()]


# Cache of every account under each manager, keyed by (identity, manager).
_tree_cache: dict = {}


def _manager_tree(manager_id: str, credentials) -> frozenset:
    """All client account ids anywhere below a manager account (cached)."""
    key = (_credential_key(credentials), manager_id)
    with _accessible_lock:
        if key in _tree_cache:
            return _tree_cache[key]
    try:
        client = GoogleAdsClient(
            credentials=credentials,
            developer_token=_get_developer_token(),
            login_customer_id=manager_id,
            use_proto_plus=True,
        )
        rows = client.get_service("GoogleAdsService").search(
            customer_id=manager_id,
            query="SELECT customer_client.id FROM customer_client",
        )
        result = frozenset(str(row.customer_client.id) for row in rows)
    except Exception as ex:  # non-fatal: this manager just won't match
        logger.warning(
            "ads_mcp: could not list accounts under manager %s: %s",
            manager_id,
            ex,
        )
        result = frozenset()
    with _accessible_lock:
        _tree_cache[key] = result
    return result


def get_googleads_client(
    login_customer_id: Optional[str] = None,
) -> GoogleAdsClient:
    credentials = _create_credentials()
    args = {
        "credentials": credentials,
        "developer_token": _get_developer_token(),
        "use_proto_plus": True,
    }

    resolved = (
        clean_customer_id(login_customer_id)
        if login_customer_id
        else resolve_login_customer_id(_target_customer_id.get(), credentials)
    )
    if resolved:
        args["login_customer_id"] = resolved

    return GoogleAdsClient(**args)


def get_googleads_service(service_name: str):
    return get_googleads_client().get_service(service_name)


def clean_customer_id(customer_id: str) -> str:
    """Normalizes '123-456-7890' style ids to '1234567890'."""
    return customer_id.replace("-", "").replace(" ", "").strip()


def micros(amount: float) -> int:
    """Converts a currency amount to the API's micros representation."""
    return int(round(float(amount) * 1_000_000))


def to_tool_error(ex: GoogleAdsException) -> ToolError:
    """Converts a GoogleAdsException into a readable ToolError."""
    error_msgs = []
    for error in ex.failure.errors:
        msg = f"Google Ads API Error: {error.message}"
        field_path = ".".join(
            el.field_name for el in error.location.field_path_elements
        )
        if field_path:
            msg += f" (field: {field_path})"
        pvd = error.details.policy_violation_details
        if pvd.key.policy_name:
            msg += (
                f" [policy: {pvd.key.policy_name}, "
                f"text: '{pvd.key.violating_text}', "
                f"exemptible: {pvd.is_exemptible}]"
            )
        error_msgs.append(msg)
    return ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs))


def _collect_exemptible_policy_keys(ex: GoogleAdsException):
    """Maps operation index -> exemptible PolicyViolationKeys.

    Returns (keys_by_operation, non_exemptible_descriptions).
    """
    keys_by_op: dict = {}
    non_exemptible = []
    for error in ex.failure.errors:
        pvd = error.details.policy_violation_details
        if not pvd.key.policy_name:
            # Not a policy violation error at all.
            non_exemptible.append(error.message)
            continue
        op_index = None
        for el in error.location.field_path_elements:
            if el.field_name == "operations":
                op_index = el.index
                break
        if pvd.is_exemptible and op_index is not None:
            keys_by_op.setdefault(op_index, []).append(pvd.key)
        else:
            non_exemptible.append(
                f"{pvd.key.policy_name}: '{pvd.key.violating_text}'"
            )
    return keys_by_op, non_exemptible


def _collect_ignorable_policy_topics(ex: GoogleAdsException):
    """Collects ad policy-finding topics (used by ad mutates).

    Returns (ignorable_topics, blocking_descriptions).
    """
    topics = []
    blocking = []
    for error in ex.failure.errors:
        pfd = error.details.policy_finding_details
        if not pfd.policy_topic_entries:
            blocking.append(error.message)
            continue
        for entry in pfd.policy_topic_entries:
            type_name = getattr(entry.type_, "name", str(entry.type_))
            if type_name == "PROHIBITED":
                blocking.append(f"prohibited policy topic: {entry.topic}")
            else:
                topics.append(entry.topic)
    return topics, blocking


def format_output_value(value: Any) -> Any:
    if isinstance(value, proto.Enum):
        return value.name
    elif isinstance(value, proto.Message):
        return proto.Message.to_dict(value)
    elif isinstance(value, PbMessage):
        return MessageToDict(value, preserving_proto_field_name=True)
    elif hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
        return [format_output_value(v) for v in value]
    else:
        return value


def format_output_row(row: proto.Message, attributes) -> dict:
    return {
        attr: format_output_value(get_nested_attr(row, attr))
        for attr in attributes
    }


def run_query(customer_id: str, query: str) -> List[dict]:
    """Runs a GAQL query via search_stream and returns formatted rows."""
    set_target_customer_id(customer_id)
    ga_service = get_googleads_service("GoogleAdsService")
    logger.info("ads_mcp query: %s", query)
    try:
        stream = ga_service.search_stream(
            customer_id=clean_customer_id(customer_id), query=query
        )
        output: List[dict] = []
        for batch in stream:
            for row in batch.results:
                output.append(format_output_row(row, batch.field_mask.paths))
        return output
    except GoogleAdsException as ex:
        raise to_tool_error(ex)


def mutate(
    service_name: str,
    request_type: str,
    method_name: str,
    customer_id: str,
    operations: list,
    validate_only: bool = False,
) -> dict:
    """Runs a mutate request and returns the created/updated resource names.

    With validate_only=True the API checks the request without applying it,
    which is the safe way to preview a change.
    """
    set_target_customer_id(customer_id)
    client = get_googleads_client()
    service = client.get_service(service_name)
    request = client.get_type(request_type)
    request.customer_id = clean_customer_id(customer_id)
    for op in operations:
        request.operations.append(op)
    request.validate_only = validate_only
    try:
        response = getattr(service, method_name)(request=request)
    except GoogleAdsException as ex:
        # Policy violations that Google marks exemptible (common for
        # healthcare-adjacent keywords) can be retried once with explicit
        # exemption keys — the same thing the Ads UI does automatically.
        keys_by_op, non_exemptible = _collect_exemptible_policy_keys(ex)
        if keys_by_op and not non_exemptible:
            try:
                for op_index, keys in keys_by_op.items():
                    request.operations[
                        op_index
                    ].exempt_policy_violation_keys.extend(keys)
            except Exception:
                raise to_tool_error(ex)
            logger.info(
                "Retrying mutate with policy exemptions for %d operation(s)",
                len(keys_by_op),
            )
        else:
            topics, blocking = _collect_ignorable_policy_topics(ex)
            applied = False
            if topics and not blocking:
                # The parameter lives on each operation (e.g.
                # AdGroupAdOperation.policy_validation_parameter).
                for op in request.operations:
                    try:
                        op.policy_validation_parameter.ignorable_policy_topics.extend(
                            topics
                        )
                        applied = True
                    except Exception:
                        break
            if not applied:
                raise to_tool_error(ex)
            logger.info("Retrying mutate ignoring policy topics: %s", topics)
        try:
            response = getattr(service, method_name)(request=request)
        except GoogleAdsException as ex2:
            raise to_tool_error(ex2)

    if validate_only:
        return {
            "validate_only": True,
            "message": "Request is valid. No changes were applied.",
        }
    return {
        "validate_only": False,
        "resource_names": [r.resource_name for r in response.results],
    }


def apply_update_mask(client: GoogleAdsClient, operation, entity) -> None:
    """Sets the operation's update_mask from the fields set on the entity."""
    from google.api_core import protobuf_helpers

    client.copy_from(
        operation.update_mask,
        protobuf_helpers.field_mask(None, entity._pb),
    )


def get_gaql_resources_filepath():
    import importlib.resources

    package_root = importlib.resources.files("ads_mcp")
    return package_root.joinpath(_GAQL_FILENAME)
