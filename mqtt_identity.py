"""Per-service MQTT credentials.

The broker gives every component its own identity with its own ACL
(`mosquitto/acl`), so "the MQTT password" is no longer a single value. That
matters most inside `main.py`, which runs six twins plus the orchestrator in
ONE process: a single shared `MQTT_USERNAME` there would force all seven to
share one identity, and the least-privilege ACL would collapse into whatever
union of permissions that identity needed.

Each client therefore looks up credentials by its own client id:

    MQTT_USERNAME_TWIN_COOLING / MQTT_PASSWORD_TWIN_COOLING
    MQTT_USERNAME_ORCHESTRATOR / MQTT_PASSWORD_ORCHESTRATOR
    ...

falling back to the plain `MQTT_USERNAME` / `MQTT_PASSWORD` pair when no
per-service variable is set. The fallback is what keeps every existing way of
running this repo working — a single HiveMQ Cloud credential, or no
credentials at all against an anonymous broker — so this change adds an
option rather than imposing one.

`run.sh` exports the per-service variables from `mosquitto/credentials.json`.
"""
import os


def env_key(client_id):
    """`twin-rack-SR-RACK-01` -> `TWIN_RACK_SR_RACK_01`.

    Environment variable names cannot contain hyphens, and the client ids
    are hyphenated, so this is the one mapping both sides must agree on.
    """
    return client_id.upper().replace("-", "_")


def credentials_for(client_id):
    """Returns (username, password) for this service, or (None, None).

    Per-service variables win; the shared pair is the fallback. A per-service
    username with no matching password is still returned as-is — mosquitto
    will reject it and the CONNACK will say so, which is a far more findable
    failure than silently connecting as somebody else.
    """
    key = env_key(client_id)
    user = os.environ.get(f"MQTT_USERNAME_{key}")
    if user:
        return user, os.environ.get(f"MQTT_PASSWORD_{key}")
    return os.environ.get("MQTT_USERNAME"), os.environ.get("MQTT_PASSWORD")


def apply_credentials(client, client_id):
    """Sets username/password on a paho client. Returns the username used.

    Returns None when no credentials are configured at all, which is a valid
    state for a local anonymous broker.
    """
    user, password = credentials_for(client_id)
    if user:
        client.username_pw_set(user, password)
    return user
