"""Config flow that deploys a Tailscale subnet-router container via Portainer."""
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema({
    vol.Required("portainer_url", default="http://192.168.2.11:9000"): str,
    vol.Required("username", default="admin"): str,
    vol.Required("password"): str,
    vol.Required("ts_authkey"): str,
    vol.Required("routes", default="192.168.2.0/24"): str,
    vol.Required("container_name", default="tailscale"): str,
})


async def _read(resp):
    try:
        return await resp.json()
    except Exception:
        return await resp.text()


class PortainerDeployFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            result = await self._deploy(user_input)
            if result.get("ok"):
                return self.async_create_entry(
                    title=f"Tailscale deployed (id {result['container_id'][:12]})",
                    data={},
                )
            errors["base"] = result.get("error", "deploy_failed")
        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def _deploy(self, d):
        base = d["portainer_url"].rstrip("/")
        name = d["container_name"]
        verify = base.lower().startswith("https")
        session = async_get_clientsession(self.hass, verify_ssl=verify)
        try:
            async with session.post(f"{base}/api/auth",
                                    json={"username": d["username"],
                                          "password": d["password"]}) as r:
                body = await _read(r)
                if r.status != 200:
                    _LOGGER.error("Portainer auth failed %s: %s", r.status, body)
                    return {"error": f"auth_failed_{r.status}"}
                jwt = body["jwt"]
            headers = {"Authorization": f"Bearer {jwt}"}

            async with session.get(f"{base}/api/endpoints", headers=headers) as r:
                eps = await _read(r)
            if not isinstance(eps, list) or not eps:
                _LOGGER.error("No Portainer endpoints: %s", eps)
                return {"error": "no_endpoints"}
            ep = eps[0]["Id"]
            docker = f"{base}/api/endpoints/{ep}/docker"

            async with session.post(f"{docker}/images/create",
                                    params={"fromImage": "tailscale/tailscale",
                                            "tag": "latest"}, headers=headers) as r:
                await r.read()
                if r.status != 200:
                    _LOGGER.error("Image pull failed %s", r.status)
                    return {"error": f"pull_failed_{r.status}"}

            definition = {
                "Image": "tailscale/tailscale:latest",
                "Hostname": name,
                "Env": [
                    f"TS_AUTHKEY={d['ts_authkey']}",
                    f"TS_ROUTES={d['routes']}",
                    "TS_STATE_DIR=/var/lib/tailscale",
                    "TS_USERSPACE=false",
                ],
                "HostConfig": {
                    "NetworkMode": "host",
                    "CapAdd": ["NET_ADMIN", "NET_RAW"],
                    "Devices": [{"PathOnHost": "/dev/net/tun",
                                 "PathInContainer": "/dev/net/tun",
                                 "CgroupPermissions": "rwm"}],
                    "RestartPolicy": {"Name": "unless-stopped"},
                    "Binds": ["tailscale-state:/var/lib/tailscale"],
                },
            }
            async with session.post(f"{docker}/containers/create",
                                    params={"name": name}, headers=headers,
                                    json=definition) as r:
                body = await _read(r)
                if r.status not in (200, 201):
                    _LOGGER.error("Create failed %s: %s", r.status, body)
                    return {"error": f"create_failed_{r.status}"}
                cid = body["Id"]

            async with session.post(f"{docker}/containers/{cid}/start",
                                    headers=headers) as r:
                if r.status not in (200, 204):
                    _LOGGER.error("Start failed %s: %s", r.status, await _read(r))
                    return {"error": f"start_failed_{r.status}"}

            _LOGGER.info("Tailscale container deployed: %s", cid)
            return {"ok": True, "container_id": cid}
        except Exception as e:  # noqa
            _LOGGER.exception("Deploy error")
            return {"error": f"exception_{type(e).__name__}"}
