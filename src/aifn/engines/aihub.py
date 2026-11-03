from urllib.parse import parse_qsl, urlencode, urlsplit


def is_aihub(base_url: str) -> bool:
    host = urlsplit(base_url.strip()).hostname or ""
    return host == "aihub.woa.com" or host.endswith(".aihub.woa.com")


def session_api_key(api_key: str, session_id: str) -> str:
    """Pin AI Hub requests to one session while preserving the key's other options."""
    if not session_id:
        raise ValueError("AI Hub cache affinity requires a session id")
    key, _, query = api_key.strip().partition("?")
    parameters = [(name, value) for name, value in parse_qsl(query, keep_blank_values=True)
                  if name != "cache_task_id"]
    parameters.append(("cache_task_id", session_id))
    return key + "?" + urlencode(parameters)
