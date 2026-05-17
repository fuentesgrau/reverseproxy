# Standard Libraries
import asyncio, logging, time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

# 3rd Party Libraries
import docker
from docker.models.containers import Container

@dataclass(slots=True)
class Session:
    container: Container  # Needed to communicate with the Container
    last_seen: float      # Needed to determine whether the session has been abandoned and therefore can be deleted
    path: Path            # Needed to delete the data directory in case the session has been abandoned
    webui_id: int         # To avoid exposing the SID in the WebUI, because as of now everybody can access the WebUI.

d = docker.from_env()
logger = logging.getLogger(__name__)

available_webui_ids = deque()
webui_id_counter = 1

def get_http_request_path(http_request_header: bytes) -> bytes:
    """Parses the path inside an HTTP-Request.

    Example for http_request_header:

    GET / HTTP/1.1\r\n
    Host: localhost:1453\r\n
    \r\n

    Then http_request_path will be:

    /

    Another example for http_request_header:

    GET /path/to/somewhere HTTP/1.1\r\n
    Host: localhost:1453\r\n
    \r\n

    Then http_request_path will be:

    /path/to/somewhere
    """
    http_request_header_fields = http_request_header.split(b'\r\n')                               # Each line in HTTP-Header always ends with CRLF. Source: RFC 9112 (Section 2.1).
    http_request_line = http_request_header_fields[0]                                             # The first line in HTTP-Header is always the Request Line. Source: RFC 9112 (Section 2.1).
    http_request_method, http_request_path, http_request_version = http_request_line.split(b' ')  # Request Line always has the scheme: Method Path Version. Source: RFC 9112 (Section 3).

    return http_request_path

def get_http_request_cookies(http_request_header: bytes) -> dict:
    """Parses cookies inside an HTTP-Request and returns them in a dictionary.

    Example for http_request_header:

    GET / HTTP/1.1\r\n
    Host: localhost:1453\r\n
    Cookie: name1=value1; name2=value2; name3=value3\r\n
    \r\n

    Then cookies will be:

    {name1: value1, name2: value2, name3: value3}
    """
    http_request_cookies = {}  # In this dictionary the parsed cookies will be stored as `cookiename: cookievalue` pairs.

    http_request_header_fields = http_request_header.split(b'\r\n')
    for http_request_header_field in http_request_header_fields:
        if http_request_header_field.lower().startswith(b'cookie:'):           # HTTP-Header names are case-insensitive. Source: RFC 9112 (Section 5).
            http_request_header_field = http_request_header_field[7:].strip()  # Remove optional leading or trailing whitespace. Source: RFC 9112 (Section 5).
            http_request_header_field = http_request_header_field.split(b';')  # Each cookie becomes a list element
            for cookie in http_request_header_field:
                name, value = cookie.strip().split(b'=', 1)
                http_request_cookies[name.decode()] = value.decode()
            break

    return http_request_cookies

def get_http_content_length(http_header: bytes) -> int:
    content_length = 0

    http_header_fields = http_header.split(b'\r\n')
    for http_header_field in http_header_fields:
        if http_header_field.lower().startswith(b'content-length:'):
            content_length = int(http_header_field[15:].strip())
            break

    return content_length

def get_webui_id():
    global webui_id_counter

    if available_webui_ids:
        webui_id = available_webui_ids.popleft()
    else:
        webui_id = webui_id_counter
        webui_id_counter += 1

    return webui_id

async def create_session(sid: str) -> Session:
    path = Path(__file__).parent.parent / 'data' / sid  # The path is needed for the automatic cleanup of abandoned sessions
    await asyncio.to_thread(path.mkdir)
    container = await asyncio.to_thread(
        d.containers.run,
        'nodered/node-red',                        # type: ignore
        detach=True,                                     # type: ignore
        ports={'1880/tcp': 0},                           # -p 0:1880 (0 lets the kernel choose a free port)
        volumes={path: {'bind': '/data', 'mode': 'rw'}}  # -v reverseproxy/data/sid:/data
    )                                                    # type: ignore
    session = Session(container, time.monotonic(), path, get_webui_id())

    return session

def is_ratelimited(ip: str, ip_ratelimits: dict) -> bool:
    current_time = time.monotonic()
    ip_ratelimit = ip_ratelimits.setdefault(ip, deque())

    # Die 60 sind die 60 Sekunden im Beispiel: maximal 20 Requests je 60 Sekunden
    while ip_ratelimit and current_time - 60 > ip_ratelimit[0]:  # Same as: current_time - ip_ratelimit[0] > 60
        ip_ratelimit.popleft()

    if len(ip_ratelimit) > 20:  # Das sind die 20 Container im Beispiel: maximal 20 Container bzw. Requests je 60 Sekunden
        return True

    ip_ratelimit.append(current_time)

    return False