# Setting it up yourself

For a person — or, more usefully, for their AI agent — building their own copy of MqttChat: a
browser page that asks a local model a question from anywhere, over MQTT.

Read this before editing anything. It explains what each piece is for, what you will have to
ask the human for, and where this repository's own deployment is hard-coded. The project's own
`README.md` describes how the finished thing is put together; this file is about getting there.

## What you are building

```
   phone / laptop                    broker                    PC with the model
  ┌──────────────┐            ┌──────────────────┐          ┌──────────────────────┐
  │ the page     │  ask ─────▶│                  │─────────▶│ mqttchat-server.as   │
  │ (static)     │            │   <topic>/chat   │          │  └ as_chat.py        │
  │              │  ◀─ chunks │                  │◀─────────│     └ Ollama         │
  └──────────────┘            └──────────────────┘          └──────────────────────┘
```

Three parts, and only one of them has a door to the internet:

1. **The page** — static files, served over HTTPS anywhere.
2. **The broker** — the only thing that accepts connections from outside. Everything else dials
   *out* to it, so nothing on the PC or in the house is reachable.
3. **The service** — on the PC, holding the model, connected out to the broker.

## Why MQTT, in plain terms

MQTT is a publish/subscribe message bus: clients connect to a **broker** and subscribe to
**topics**; anything published to a topic is delivered to whoever subscribed. It is small, it
was designed for flaky links, and it has been the plumbing behind telemetry and home automation
for years.

It is used here for one reason: **it is a meeting point that both ends can reach without either
being reachable**. The phone publishes a question to a topic; the PC, which subscribed, is
handed it. The answer is published to a topic the phone subscribed to. Neither end opens a port,
so there is nothing in the house for the internet to find.

## What you will need

| You need | Why | What it costs |
|---|---|---|
| A PC with Ollama | it answers the questions | already have it |
| A small Linux server | to run the broker (and whatever else you like) | ~$10/month |
| A hostname | TLS certificates need one; WhatsApp-style IP-address clients do not | a few $/year, or free |
| Somewhere HTTPS to serve two files | the page and its script | the same server will do |

**Do not use a free public broker.** There are shared brokers (test.mosquitto.org, the free
tiers of commercial ones) and they are excellent for trying MQTT out, but they are shared and
unauthenticated-by-default: your questions and answers would be visible to strangers, you cannot
control who subscribes, and free tiers throttle. This is a private chat with your own machine;
it wants its own broker.

**Mosquitto** is the broker software — the standard one, the same software this project runs.
The "rent a server" advice is not about the software; it is about where it lives. A 1 vCPU,
2 GB server is ample, and it can host other things at the same time (this one runs a
document service, Zigbee telemetry and more).

## 1. The broker

On the server: install `mosquitto`, then point it at a hostname with a certificate from Let's
Encrypt (certbot does both). Browsers insist on TLS for websockets, so two listeners are needed:
ordinary MQTT on 8883 for scripts, and websockets proxied behind nginx on 443 for the page. The
whole of the broker's own configuration is this:

```
# /etc/mosquitto/conf.d/chat.conf
allow_anonymous false
listener 1883 localhost              # local use, if the server wants any
listener 8883
certfile /etc/letsencrypt/live/<hostname>/fullchain.pem
cafile   /etc/letsencrypt/live/<hostname>/chain.pem
keyfile  /etc/letsencrypt/live/<hostname>/privkey.pem
listener 8084
protocol websockets                  # nginx proxies wss://<hostname>/mqtt here
password_file /etc/mosquitto/passwd
acl_file /etc/mosquitto/aclfile
```

and nginx needs one location, because a browser cannot talk MQTT directly:

```
location /mqtt {
    proxy_pass http://localhost:8084;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "Upgrade";
}
```

## 2. Two MQTT accounts, not one

The page's credentials are **public**: the page has to connect to the broker itself, so anyone
who loads it can read the username and password out of the network traffic. That is fine, as
long as those credentials can only reach the chat topic. So make a second account and fence it
with an ACL:

```
# /etc/mosquitto/aclfile
user <house-account>            # whatever else uses this broker, if anything
topic readwrite #

user chat                       # the public page, and the chat service
topic readwrite <topic-prefix>/chat/#
```

Create the account with `mosquitto_passwd -b /etc/mosquitto/passwd chat <password>`, then
`systemctl reload mosquitto`.

If the broker is dedicated to this project, one account is enough — but a public page plus a
house account on one broker is exactly the case where this matters, and it costs four lines.
Note the topic design that follows from it: **the page's reply topic lives underneath the chat
topic** (`<topic-prefix>/chat/reply-<random>`), because a reply topic outside the namespace the
ACL grants would never be delivered. Change one and you must change the other.

### Three things that cost real time here

**Reply topics have to live in a namespace.** An ACL cannot express a name *prefix*: MQTT wildcards match whole levels, so `Doclets-+` is invalid — mosquitto rejects the entire ACL file, not just that line — and `Doclets-123456` has no filter that covers it. Name each client's reply topic inside a namespace (`doclets/reply/<id>`, `<topic>/chat/reply-<id>`) so one `.../reply/#` line covers all of them.

**`#` does not cover `$SYS`.** If anything on the broker is a mosquitto *bridge*, it announces itself on `$SYS/broker/connection/<client id>/state`, and an ACL replaces the default of "anything goes" with "only what is listed". Without an explicit `topic readwrite $SYS/#` for that account, the bridge loses its own state topic — silently, apart from a `Denied PUBLISH` in the log.

**Validate an ACL before applying it.** A reload that fails to parse the ACL leaves mosquitto running with *no* ACL at all, which is worse than the one you were replacing: every account is then unrestricted, and nothing says so. Copy the candidate to a scratch path, run a second mosquitto on a spare port with `/tmp/candidate.conf`, and try the allowed and denied cases there first.

## 3. The access token

Separately again: an **access token** — a shared secret the page asks for once per device and
the service checks on every question. With the credentials public, this is what actually stops a
stranger using your GPU. It lives in a file beside the service (`token`, mode 600), or in
`MQTTCHAT_TOKEN`, and never in the page.

Rotating it (change the file, restart the service) is how you cut off a device: the token stored
in its browser stops being accepted, the page forgets it, and it asks for the new one.

## 4. The service, on the PC

Copy nothing: run it from the project directory. It reads `mqttchat-server.as` and `as_chat.py`
from its working directory, so a checkout that both machines can see is simplest.

```
mkdir -p ~/.config/systemd/user
ln -sf ~/dev/mqttchat/mqttchat.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mqttchat
loginctl enable-linger $USER        # keep it running without a login, and at boot
```

Settings live in the environment (`mqttchat.service` lists them, `README.md` documents them).
The two that matter first: `MQTTCHAT_MODEL` must be a model the PC has actually pulled
(`ollama list`), and `MQTTCHAT_KEEP_ALIVE` decides how long the model stays loaded between
questions. The service waits a couple of minutes for its first answer if the model has to load.

## 5. The page

Two files that a browser fetches (`mqttchat.html`, `mqttchat-main.as`), the layout
(`mqttchat.json`), and the PWA extras (manifest, service worker, icons). `deploy.sh` copies
them; `--infra` also adds `.htaccess` (so the manifest is served as JSON) and
`credentials.php`.

The page needs one thing from the server: a JSON file holding the broker host, the `chat`
account and the topic. `credentials.php` serves it from *outside* the web root, which keeps it
out of reach of a directory listing; a static file would do as well, since it is public anyway.

## 6. What your agent will ask you for

Hand it these when the questions come, rather than letting it guess:

- [ ] the broker's **hostname**, and confirmation it is up (`openssl s_client -connect <host>:8883`)
- [ ] a **topic prefix** for this installation, e.g. the machine's MAC address, or anything unique
- [ ] the **`chat` account's password**, and the ACL line granting it `<topic>/chat/#`
- [ ] the **web host and path** the page is deployed to, over HTTPS
- [ ] an **access token** — either one you choose, or let it generate a long random one
- [ ] which **model** Ollama has pulled on the PC (`ollama list`)
- [ ] the PC's **LAN address and login**, so it can install and restart the service

## 7. Where this repository is hard-coded

If you are adapting *this* copy rather than starting from the starter, these are the places the
original deployment is baked in — change all of them or none:

| File | What to change |
|---|---|
| `mqttchat-main.as` | the credentials URL it fetches (and the localhost fallback) |
| `mqttchat.json`, `mqttchat.html`, `mqttchat.webmanifest` | nothing — but the icons and title are personal taste |
| `mqttchat.service` | `WorkingDirectory`, `ExecStart`, `MQTTCHAT_MODEL` |
| `credentials.php`, `chat.example.com.txt.example` | the host the credentials file is named after |
| `deploy.sh`, `deploy.conf.example` | the host and path the page is deployed to |
| `README.md`, `DIFF.md` | the deployment story — the starter has its own |

## 8. Testing it without a browser

Framing is the detail that costs the most time here. Every AllSpeak MQTT client frames its payload, and the receiver **drops anything unframed**, warning loudly in its log and taking no further action — so a question sent with `mosquitto_pub` will look as though it vanished.

The rule has two forms. A payload that fits in one chunk travels as `!last!<total> <data>`; a larger one — the page splits at 1024 bytes — travels as `!part!<n> <total> <data>` for every chunk but the last, then `!last!<total> <data>` to finish. The receiver reassembles the parts before anyone sees them, so this only matters to a publisher that is not AllSpeak. The example below is small enough to be a single chunk:

```
mosquitto_pub -h <host> -p 8883 -u chat -P '<password>' \
  -t '<topic>/chat' -m '!last!1 {"sender":{"name":"<topic>/chat/reply-test","qos":1},"action":"ask","message":"<token>\nHello"}'
mosquitto_sub -h <host> -p 8883 -u chat -P '<password>' -t '<topic>/chat/reply-test' -v
```

## 9. What the code is written in

AllSpeak, a scripting language meant to read as English, with a browser runtime and a Python one.
Two consequences for whoever edits it:

- **Every section carries a doc block** (`!!` prose … `!!!`) saying why it exists, and
  `python3 asdoc-check.py --write <file>` refreshes the hashes that detect drift between prose
  and code. `AGENTS.md` in this directory is the contract the agents follow.
- **The language has sharp edges** that catch AI writers reliably, and the project's own
  `AGENTS.md` lists them. The three that cost real time here: `cat` is infix and never leads;
  `clear` on a text variable leaves *false*, not empty (use `put empty into`); and in the Python
  runtime a condition like `is object` never holds, so type checks belong in a plugin where
  Python's own `isinstance` is available.
