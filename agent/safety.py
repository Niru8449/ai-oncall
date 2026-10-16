import sys
from pathlib import Path
from urllib.parse import urlparse

from kubernetes import client, config

SANDBOXES = Path.home() / ".oncall" / "sandboxes"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
MARKER_NAMESPACE = "storefront"
MARKER_NAME = "oncall-sandbox"


class Refused(Exception):
    pass


def refuse(reason):
    raise Refused(f"Refusing to run: {reason}")


def connect_to_sandbox(name):
    home = SANDBOXES / name
    kubeconfig = home / "agent.kubeconfig"
    if not kubeconfig.is_file():
        refuse(f"no sandbox called {name!r}. Only harness/sandbox.py creates sandboxes.")
    context = f"kind-{name}-agent"
    try:
        api = config.new_client_from_config(config_file=str(kubeconfig), context=context)
    except config.ConfigException:
        refuse(f"{kubeconfig} has no context {context!r}.")
    host = urlparse(api.configuration.host).hostname
    if host not in LOCAL_HOSTS:
        refuse(f"the API server {host!r} is not on this Mac.")
    assert_same_cluster(api, home)
    return api


def assert_same_cluster(api, home):
    expected = (home / "id").read_text().strip()
    try:
        marker = client.CoreV1Api(api).read_namespaced_config_map(MARKER_NAME, MARKER_NAMESPACE)
    except client.ApiException:
        refuse("this cluster has no oncall-sandbox marker.")
    if (marker.data or {}).get("id") != expected:
        refuse("this cluster's ID doesn't match the sandbox's.")


def main(argv=None):
    name = (argv or sys.argv[1:] or ["oncall"])[0]
    try:
        api = connect_to_sandbox(name)
    except Refused as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"connected to sandbox {name!r} as the agent: {api.configuration.host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
