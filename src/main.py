# Standard Libraries
import asyncio, json, logging, signal, shutil, sys, time, uuid
from asyncio import StreamReader, StreamWriter, IncompleteReadError
from pathlib import Path

# Local Libraries
import webui
from utils import (
    available_webui_ids,
    create_session,
    get_http_content_length,
    get_http_request_cookies,
    get_http_request_path,
    is_ratelimited
)

# Filtering unavoidable error messages, because they happen way too deep down in the lower levels of asyncio
class LoggingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return (
            'Fatal read error on socket transport' not in message and  # Happens when trying to write on a Docker-Container which isn't ready yet
            'Fatal write error on socket transport' not in message     # Happens when the Client closes the WebUI (SSE connection)
        )
logging.getLogger('asyncio').addFilter(LoggingFilter())
logger = logging.getLogger(__name__)

ip_ratelimits = {}
sessions = {}

def graceful_shutdown():
    for session in sessions.values():
        logger.debug(f"Deleting Session: {session}")
        session.container.stop()
        session.container.remove()
        shutil.rmtree(session.path, ignore_errors=False, onerror=None)
        # Didn't do `del sessions[sid]` since the program is going to close anyway
    sys.exit(0)

async def poll_ip_ratelimits():
    while True:
        await asyncio.sleep(600)
        for ip, ip_ratelimit in list(ip_ratelimits.items()):
            if len(ip_ratelimit) == 0:
                del ip_ratelimits[ip]

async def poll_sessions():
    while True:
        # A *too tight* window leads to the deletion of the container before it can be even used
        await asyncio.sleep(60)
        for sid, session in list(sessions.items()):  # list(...) erstellt eine Kopie. Lösung dafür, dass in der Schleife `del sessions[sid]` aufgerufen werden muss.
            elapsed_time = time.monotonic() - session.last_seen
            logger.debug(f"Elapsed Time: {elapsed_time} seconds for Session: {session}")
            if elapsed_time > 60:
                logger.debug(f"Deleting expired Sessoin: {session}")
                await asyncio.to_thread(session.container.stop)
                await asyncio.to_thread(session.container.remove)
                await asyncio.to_thread(shutil.rmtree, session.path, ignore_errors=False, onerror=None)
                available_webui_ids.append(session.webui_id)
                del sessions[sid]

async def client_connected_cb(client_reader: StreamReader, client_writer: StreamWriter) -> None:
    if is_ratelimited(client_writer.get_extra_info('peername')[0], ip_ratelimits):
        client_writer.write(b'HTTP/1.1 429 Too Many Requests\r\nRetry-After: 60\r\n\r\n')
        await client_writer.drain()

        client_writer.close()
        await client_writer.wait_closed()

        return

    try:
        # HTTP-Header and HTTP-Body are always separated by a blank line: \r\n\r\n. Source: RFC 9112 (Section 2.1).
        http_request_header = await asyncio.wait_for(client_reader.readuntil(b'\r\n\r\n'), timeout=1)  # If necessary, set a longer timeout value
    except IncompleteReadError as e:
        logger.debug(f"IncompleteReadError: {e}")
        logger.debug(f"Read Message: {e.partial}")

        client_writer.write(b'HTTP/1.1 400 Bad Request\r\n\r\n')
        await client_writer.drain()

        client_writer.close()
        await client_writer.wait_closed()

        return
    logger.debug(f"HTTP Request Header: {http_request_header}")

    http_request_path = get_http_request_path(http_request_header)
    if http_request_path == b'/webui':
        http_response = await webui.get_html()

        client_writer.write(http_response)
        await client_writer.drain()

        client_writer.close()
        await client_writer.wait_closed()

        return
    elif http_request_path == b'/favicon.ico':
        http_response = await webui.get_favicon()

        client_writer.write(http_response)
        await client_writer.drain()

        client_writer.close()
        await client_writer.wait_closed()

        return
    elif http_request_path == b'/webui.js':
        http_response = await webui.get_webui_js()

        client_writer.write(http_response)
        await client_writer.drain()

        client_writer.close()
        await client_writer.wait_closed()

        return
    elif http_request_path == b'/events':
        http_response = webui.get_events()

        client_writer.write(http_response)
        while True:
            data = [session.webui_id for session in sessions.values()]
            data = f"data: {json.dumps(data)}\n\n"  # Liste data wird in JSON Syntax umformuliert

            client_writer.write(data.encode())

            # Diese Exception tritt i.d.R. auf, wenn die Verbindung unterbrochen wurde bspw. Client hat Tab geschlossen.
            try:
                await client_writer.drain()
            except ConnectionResetError as e:
                logger.debug(f"ConnectionResetError: {e}")

                client_writer.close()
                return

            await asyncio.sleep(3)

    http_request_cookies = get_http_request_cookies(http_request_header)
    sid = http_request_cookies.get('sid')  # sid stands for "session id"

    if sid in sessions:
        content_length = get_http_content_length(http_request_header)
        http_request_body = await client_reader.readexactly(content_length)

        logger.debug(f"HTTP Request Body: {http_request_body}")

        port = sessions[sid].container.ports['1880/tcp'][0]['HostPort']
        container_reader, container_writer = await asyncio.open_connection('localhost', port)

        container_writer.write(http_request_header + http_request_body)
        await container_writer.drain()

        async def forward(reader: StreamReader, writer: StreamWriter):
            while True:
                sessions[sid].last_seen = time.monotonic()
                message = await reader.read(4096)
                if not message:
                    break
                writer.write(message)
                await writer.drain()
            writer.close()
            await writer.wait_closed()

        await asyncio.gather(
            forward(client_reader, container_writer),
            forward(container_reader, client_writer)
        )

        return
    else:
        sid = uuid.uuid4().hex  # Linter warns unnecessarily when I use str(uuid.uuid4())
        sessions[sid] = await create_session(sid)

        await asyncio.to_thread(sessions[sid].container.reload)
        port = sessions[sid].container.ports['1880/tcp'][0]['HostPort']

        container_reader, container_writer = await asyncio.open_connection('localhost', port)
        http_response_header = b''  # Just to calm down the linter
        while True:
            try:
                container_writer.write(http_request_header)
                await container_writer.drain()

                http_response_header = await container_reader.readuntil(b'\r\n\r\n')
            except ConnectionResetError as e:
                logger.debug(f"ConnectionResetError: {e}")

                container_writer.close()

                await asyncio.sleep(3)

                container_reader, container_writer = await asyncio.open_connection('localhost', port)
                continue
            except IncompleteReadError as e:
                logger.debug(f"IncompleteReadError: {e}")

                container_writer.close()
                await container_writer.wait_closed()

                await asyncio.sleep(3)

                container_reader, container_writer = await asyncio.open_connection('localhost', port)
                continue
            break
        http_response_header = http_response_header.replace(b'\r\n\r\n', f"\r\nSet-Cookie: sid={sid}; SameSite=Strict; HttpOnly\r\n\r\n".encode(), 1)

        content_length = get_http_content_length(http_response_header)
        http_response_body = await container_reader.readexactly(content_length)

        logger.debug(f"HTTP Response Header: {http_response_header}")
        logger.debug(f"HTTP Response Body: {http_response_body}")

        client_writer.write(http_response_header + http_response_body)
        await client_writer.drain()  # At this point, the Browser will fire up multiple TCP connections and request the referenced HTML, CSS, JS etc. files

        client_writer.close()
        await client_writer.wait_closed()

        container_writer.close()
        await container_writer.wait_closed()

        return

async def main():
    asyncio.get_event_loop().add_signal_handler(signal.SIGINT, graceful_shutdown)        # type: ignore
    socket = await asyncio.start_server(client_connected_cb, '0.0.0.0', 1453)  # default StreamReader buffer limit is 64 KiB
    async with socket:
        await asyncio.gather(
            socket.serve_forever(),
            poll_ip_ratelimits(),
            poll_sessions()
        )

if __name__ == '__main__':
    path = Path(__file__).parent.parent / 'logs' / 'main.log'
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(path, mode='w'), logging.StreamHandler()]
    )
    asyncio.run(main(), debug=True)