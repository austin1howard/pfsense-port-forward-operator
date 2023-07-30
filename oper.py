"""
KOPF operator for adding port-forward rules to PFSense. Requires https://github.com/jaredhendrickson13/pfsense-api to be installed.
"""
import asyncio
from typing import Any

import aiohttp
import kopf
from pydantic import SecretStr
from pydantic_settings import BaseSettings

# Set this annotation to "all" to forward all ports without changing value
# Set this annotation to "none" to remove port forwarding (or remove the annotation)
# Set this annotation to string like "80:8080,443:8443" to forward ports 80 and 443 to 8080 and 8443 respectively
ANNOTATION = "k8s.ahoward.io/port-forward"


class RuntimeSettings(BaseSettings):
    base_url: str
    api_client_id: str
    api_token: SecretStr
    public_ingress_ip: str
    pfsense_log_only: bool = False


runtime_settings = RuntimeSettings()

rule_update_lock: asyncio.Lock


def _authenticated_session():
    # use json content type and the api token
    return aiohttp.ClientSession(
        headers={
            "Content-Type": "application/json",
            "Authorization": f"{runtime_settings.api_client_id} {runtime_settings.api_token.get_secret_value()}",
        }
    )


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    # After sitting idle for a while (few days) the kopf operator hangs and stops receiving new events. As per info in
    # https://github.com/nolar/kopf/issues/585#issuecomment-735482328 these result in a hard-reconnection every ten minutes,
    # so we aren't long polling for days on end
    settings.watching.client_timeout = 600
    settings.watching.server_timeout = 600

    global rule_update_lock
    rule_update_lock = asyncio.Lock()


@kopf.on.create("services", annotations={ANNOTATION: kopf.PRESENT})
async def add_port_forward(logger, annotations, body, **kwargs):
    """
    Add a port forwarding rule to pfsense with the REST api
    """
    # Determine the needed port mapping
    port_mapping = _determine_port_mapping(body, annotations[ANNOTATION])

    # Get the IP address assigned to the service by the load balancer. Temp error if it isn't assigned yet.
    private_target_ip = _get_target_ip(body)

    # Add the port forward rules
    # Use a shared aiohttp session to avoid creating a new connection for each rule.
    async with _authenticated_session() as session:
        for source_port, (destination_port, protocol) in port_mapping.items():
            description = _create_rule_description(body, source_port, destination_port, protocol)
            json_body = _make_json_body(protocol, source_port, private_target_ip, destination_port, description)
            await _create_rule(json_body, description, session, logger)

        # Apply the rules
        await _apply_updates(session, logger)


@kopf.on.update("services", annotations={ANNOTATION: kopf.PRESENT})
async def update_port_forward(logger, annotations, body, **kwargs):
    """
    Check the port forwarding rules in pfsense. Keep the ones that match, delete the ones that don't, and add the ones that are new
    """
    # Determine the needed port mapping
    port_mapping = _determine_port_mapping(body, annotations[ANNOTATION])

    # Get the IP address assigned to the service by the load balancer. Temp error if it isn't assigned yet.
    private_target_ip = _get_target_ip(body)

    # Use a shared aiohttp session to avoid creating a new connection for each rule.
    async with _authenticated_session() as session:
        # Get the existing port forward rules
        existing_rules_dict = await _get_existing_rules_dict(session)

        # And generate a list of descriptions that should exist
        needed_rules_dict = {
            _create_rule_description(body, source_port, destination_port, protocol): (source_port, destination_port, protocol)
            for source_port, (destination_port, protocol) in port_mapping.items()
        }

        # Get the descriptions that need to be added, removed, and unchanged
        descriptions_to_add = needed_rules_dict.keys() - existing_rules_dict.keys()
        descriptions_to_remove = existing_rules_dict.keys() - needed_rules_dict.keys()

        # Remove the rules that aren't needed anymore
        for description in descriptions_to_remove:
            await _delete_rule(existing_rules_dict[description], session, logger)

        # Add the rules that are needed
        for description in descriptions_to_add:
            source_port, destination_port, protocol = needed_rules_dict[description]
            json_body = _make_json_body(protocol, source_port, private_target_ip, destination_port, description)
            await _create_rule(json_body, description, session, logger)

        # Apply the rules
        await _apply_updates(session, logger)


@kopf.on.delete("services", annotations={ANNOTATION: kopf.PRESENT})
@kopf.on.update("services", annotations={ANNOTATION: kopf.ABSENT})
async def update_port_forward(logger, body, **kwargs):
    """
    Remove any forwarding rules that may have been associated with this service
    """
    # Use a shared aiohttp session to avoid creating a new connection for each rule.
    async with _authenticated_session() as session:
        # Get the existing port forward rules
        existing_rules_dict = await _get_existing_rules_dict(session)

        # Remove any rules that match this service
        for description, rule in existing_rules_dict.items():
            if _check_rule_description_matches(body, description):
                await _delete_rule(rule, session, logger)

        # Apply the changes
        await _apply_updates(session, logger)


def _get_target_ip(service_body):
    """
    Get the IP address assigned to the service by the load balancer. Temp error if it isn't assigned yet.
    """
    try:
        private_target_ip = service_body["status"]["loadBalancer"]["ingress"][0]["ip"]
    except KeyError:
        raise kopf.TemporaryError("IP address not yet assigned to service", delay=10)
    return private_target_ip


def _create_rule_description(service_body, source_port, destination_port, protocol):
    """
    Make a rule description for the port forward that's human friendly but unique in pfsense
    """
    description = (
        f"{service_body['metadata']['namespace']}/{service_body['metadata']['name']} | {source_port} -> {destination_port} ({protocol}) "
        f"(created by pfsense-port-forward-operator, do not edit)"
    )
    return description


def _check_rule_description_matches(service_body, description):
    """
    Checks if the given description is associated with the given service
    """
    return description.startswith(f"{service_body['metadata']['namespace']}/{service_body['metadata']['name']} | ")


def _is_managed_rule(description):
    """
    Checks if this rule is one we manage.
    """
    return description.endswith("(created by pfsense-port-forward-operator, do not edit)")


def _determine_port_mapping(service_body: dict, annotation_value: str | None) -> dict[int, tuple[int, str]]:
    """
    Determine the port mapping for a service, given its body and the annotation.

    - If annotation value is something like "80:8080,443:8443", forward the ports to the service ports
    - If annotation value is "all", forward all ports to the service 1 to 1
    - If annotation value is "none", remove all port forwards for the service

    Returns a map of source to destination port and protocol
    """
    # At least one port is needed on the service
    service_ports = service_body["spec"]["ports"]
    if service_ports is None or len(service_ports) == 0:
        raise kopf.PermanentError("Exactly one port must be defined for service")

    if annotation_value == "all":
        return {port["port"]: (port["port"], port["protocol"]) for port in service_ports}
    elif annotation_value == "none":
        return {}
    else:
        # Parse the annotation value into a map of source to destination port and protocol
        port_mapping: dict[int, tuple[int, str]] = {}
        for port_mapping_str in annotation_value.split(","):
            source_port, destination_port = port_mapping_str.split(":")

            # confirm ports are numbers
            try:
                source_port = int(source_port)
                destination_port = int(destination_port)
            except ValueError:
                raise kopf.PermanentError(f"Annotation value does not contain valid port mapping: {annotation_value}")

            # get protocol from matching service port
            protocol = next((port["protocol"] for port in service_ports if port["port"] == int(destination_port)), None)
            if protocol is None:
                raise kopf.PermanentError(f"Service does not expose port {destination_port}")
            port_mapping[source_port] = (destination_port, protocol)

        return port_mapping


def _make_json_body(protocol: str, source_port: int, target_ip: str, target_port: int, description: str) -> dict[str, Any]:
    """
    Generate the JSON body for a port forwarding rule.
    """
    json_body = {
        "interface": "WAN",
        "protocol": protocol.lower(),
        "src": "any",
        "srcport": "any",
        "dst": runtime_settings.public_ingress_ip,
        "dstport": source_port,
        "target": target_ip,
        "local-port": target_port,
        "descr": description,
        "top": True,
    }

    return json_body


async def _create_rule(json_body, description, session, logger):
    if runtime_settings.pfsense_log_only:
        logger.info(f"Would have created port forward rule `{description}` with body: {json_body}")
        return
    async with rule_update_lock:
        async with session.post(f"{runtime_settings.base_url}/api/v1/firewall/nat/port_forward", json=json_body) as response:
            # Our error is permanent
            if response.status == 400:
                raise kopf.PermanentError(f"Failed to create port forward rule: {response.status} {response.reason}")
            elif not response.ok:
                raise kopf.TemporaryError(f"Failed to create port forward rule: {response.status} {response.reason}")
            else:
                logger.info(f"Created port forward rule `{description}`")

        # pfsense is old and slow. Give it a moment to catch up
        await asyncio.sleep(2)


async def _delete_rule(rule, session, logger):
    if runtime_settings.pfsense_log_only:
        logger.info(f"Would have deleted port forward rule `{rule['descr']}` with body: {rule}")
        return
    # Rule IDs are not stable, they are the index that pfsense has _right now_. This means we need to avoid race conditions _and_ requery
    # just to be sure. Yay for old software.
    async with rule_update_lock:
        # Get the current state of rules
        existing_rules = await _get_existing_rules(session)

        # Find the matching one
        existing_rules_descriptions = [r["descr"] for r in existing_rules]
        try:
            rule_id = existing_rules_descriptions.index(rule["descr"])
        except ValueError:
            # Weird, this should exist.
            logger.warning(f"Could not find rule `{rule['descr']}` to delete.")
            return

        # Remove the identified rule
        async with session.delete(f"{runtime_settings.base_url}/api/v1/firewall/nat/port_forward", json={"id": rule_id}) as response:
            if response.status == 400:
                raise kopf.PermanentError(f"Failed to delete port forward rule: {response.status} {response.reason}")
            elif not response.ok:
                raise kopf.TemporaryError(f"Failed to delete port forward rule: {response.status} {response.reason}")
            else:
                logger.info(f"Deleted port forward rule `{rule['descr']}`")

        # pfsense is old and slow. Give it a moment to catch up
        await asyncio.sleep(2)


async def _apply_updates(session, logger):
    if runtime_settings.pfsense_log_only:
        logger.info(f"Would have applied updates")
        return
    async with rule_update_lock:
        async with session.post(f"{runtime_settings.base_url}/api/v1/firewall/apply") as response:
            if response.status == 400:
                raise kopf.PermanentError(f"Failed to apply updates: {response.status} {response.reason}")
            elif not response.ok:
                raise kopf.TemporaryError(f"Failed to apply updates: {response.status} {response.reason}")
            else:
                logger.info(f"Applied updates")

        # pfsense is old and slow. Give it a moment to catch up
        await asyncio.sleep(2)


async def _get_existing_rules(session) -> list[Any]:
    async with session.get(f"{runtime_settings.base_url}/api/v1/firewall/nat/port_forward") as response:
        if not response.ok:
            raise kopf.TemporaryError(f"Failed to get port forward rules: {response.status} {response.reason}")
        return (await response.json())["data"]


async def _get_existing_rules_dict(session) -> dict[str, Any]:
    """
    Gets the existing rules (that would be managed by us). Returns a dict of description to rule.
    """
    existing_rules = await _get_existing_rules(session)

    # Map rules based on description
    existing_rules_dict = {rule["descr"]: rule for rule in existing_rules if _is_managed_rule(rule["descr"])}
    return existing_rules_dict
