# What is this about?

This Reverseproxy spawns for any connecting client its own Node-RED Docker-Container. 

The following video is supposed to show the basic way this Reverseproxy works.

https://github.com/user-attachments/assets/8e0c54d3-5700-47f5-aca5-c39d097d5054

Video description:
- Assume a Client (labeled Client 1) connects to the Reverseproxy (via a Browser such as Firefox).
- Upon calling a *website* the Browser automatically sends a so called HTTP-Request.
- The Reverseproxy receives the HTTP-Request and starts a Docker-Container (labeled Container 1).
- The Reverseproxy forwards the HTTP-Request to Container 1.
- Container 1 receives the HTTP-Request and replies with a so called HTTP-Response.
- The Reverseproxy receives the HTTP-Response and forwards it to Client 1.
- Thus, the Reverseproxy has established a Connection between Client 1 and Container 1.
- Let's assume a second Client (labeled Client 2) connects to the Reverseproxy while Client 1 is still connected as well.
- The Reverseproxy spawns a second Docker-Container (labeled Container 2) for Client 2.
- Now, Client 2 and Container 2 speak with each other and Client 1 and Container 1 speak with each other through the Reverseproxy.
- If one of the Clients disconnects (e.g. Client 1), then the Reverseproxy also deletes the Container associated with that Client.

# How to install and run?

> [!TIP]
> As of now, I'm mainly using Arch Linux. Thus, adjust the steps in this guide to your OS if necessary.

First of all the **Requirements**:
- a terminal of your choice (e.g. `kitty`),
- `python`,
- `docker`.

> [!IMPORTANT]
> Make sure the docker daemon is running, e.g.: `sudo systemctl enable docker --now`.

> [!IMPORTANT]
> Add docker to your user group in order to execute docker commands without sudo, e.g.: `sudo useradd -aG docker $USER`.
> Reboot your machine or relogin to make this change effective.

Second of all, download this repository. One way is to type in your terminal the following command:

```shell
git clone https://github.com/bekiralti/reverseproxy.git
```

Then change into the directory:

```shell
cd reverseproxy
```

Then create a Python virtual environment (so that the additionally installed packages required by this Reverseproxy do not mess up your system Python installation):

```shell
python -m venv .venv
```

> [!NOTE]
> The `.venv` is the directory name, you can choose whatever name you want.

Then *activate* the venv:

```shell
source .venv/bin/activate
```

Then install this program:

```shell
pip install .
```

> [!NOTE]
> You can use `pip install -e .` instead if you want to change things in the Code and see its effects immediately without having to reinstall.

Then run this program:

```shell
python ./src/main.py
```

## Basic example

Once you run this program

```shell
python ./src/main.py
```

you can test it locally by opening up a Browser of your choice (e.g. Firefox) and typing in `localhost:1453`. 
After a short while you should see a Node-RED Docker-Container loading up. 
To simulate a second Client you can open another Browser in incognito mode and type in `localhost:1453`.

You might have to adjust the following line if you want to go *public*:

```python
s = await asyncio.start_server(client_connected_cb, '0.0.0.0', 1453)
```

## Using a custom Docker-Image

Instead of the default Node-RED Docker-Container you can try to use any other Docker Image of your choice. 
You just have to change the Image name in the following part of the code:

```python
docker_container = await asyncio.to_thread(
    docker_client.containers.run,
    'nodered/node-red',
    detach=True,
    ports={'1880/tcp': 0},
    volumes={path: {'bind': '/data', 'mode': 'rw'}}
)
```

Change `ǹodered/node-red` to any Docker-Image you would like to try out, e.g. `my-docker-image`.

```python
docker_container = await asyncio.to_thread(
    docker_client.containers.run,
    'my-docker-image',
    detach=True,
    ports={'1880/tcp': 0},
    volumes={path: {'bind': '/data', 'mode': 'rw'}}
)
```

> [!NOTE]
> Since Node-RED speaks HTTP I suppose any other Docker-Image that also can speak HTTP should hopefully work flawlessly.

## Abandoned Sessions

This is the Code for polling sessions and checking if they can be deleted in order to save on memory:

```python

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
```

As of now this Reverseproxy checks every 60 seconds for *abandoned* sessions. This is done by the line:

```python
await asyncio.sleep(60)
```

You can change that value to your liking.

A session is considered abandoned when the last message between Client and the Container has been more than 60 seconds ago.
This is done by the lines:

```python
elapsed_time = time.monotonic() - session.last_seen
if elapsed_time > 60:
```

You can change the 60 seconds to your liking.

> [!IMPORTANT]
> At least in Node-RED the WebSocket heartbeat is sent about every 15 seconds.

## IP Rate Limiting

In `src/utils.py` you can adjust the amount of requests per seconds in this function:

```python
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
```

The `60` within the while loop stands for 60 seconds and the `20` in the if check stands for the amount of requests,
i.e. `20` requests per `60` seconds.

The following functions deletes *abandoned* ip addresses every 10 minutes:

```python
async def poll_ip_ratelimits():
    while True:
        await asyncio.sleep(600)
        for ip, ip_ratelimit in list(ip_ratelimits.items()):
            if len(ip_ratelimit) == 0:
                del ip_ratelimits[ip]
```

## WebUI

There is also a WebUI simply displaying the Clients and Containers in a diagram. 
If you are testing locally just type in your Browser: `loocalhost:1453/webui`

> [!NOTE]
> Since I am still not very familiar with JavaScript, I had to vibecode the JavaScript file. 
> This means, it needs to be rewritten. Maybe with React, if it makes things easy?

# What is next?

Well, the following bullet points could be a plausible roadmap:

- Advance the socket from HTTP to HTTPS.
- Rewrite the *frontend* JavaScript code (, because it is 100 % LLM at this point of time) and make it more like in the animation.
- Remove (or add) logging messages.
- Only send SSE when `data` changes.
- Write test scripts.

# How this Reverseproxy was built?

If you are interested on the thought processes, reasoning and learning that went into this project,
then feel free to read through [./docs/implementation_journal.md](./docs/implementation_journal.md).

> [!NOTE]
> The final source code differs from the Code snippets shown however the idea is fundamentally the same.
> I structured the actual Source Code a little bit more than what is shown in the referenced Implementation Journal.