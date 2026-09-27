# MqttChat

Ask a local model a question from a phone or laptop, anywhere, without opening a port to
the PC. The page and the PC never talk to each other directly: both connect out to the MQTT
broker, which is already reachable from outside the network.

```
        phone / laptop                          broker                       PC (GPU + Ollama)
  ┌─────────────────────────┐            ┌──────────────────┐          ┌───────────────────────────┐
  │ mqttchat.html           │  ask ─────▶│                  │─────────▶│ mqttchat-server.allspeak  │
  │ mqttchat-main.allspeak  │            │  <topic>/chat    │          │  └ as_chat.py             │
  │ mqttchat.json           │  ◀─ chunks │                  │◀─────────│     └ Ollama /api/chat    │
  └─────────────────────────┘            └──────────────────┘          └───────────────────────────┘
           wss://443                          tls://8883                       http://11434
```

The answer streams back chunk by chunk, so it types out as the model produces it.

## Files

| File | Role |
|---|---|
| `mqttchat.html` | Launcher: loads the AllSpeak bundle + MQTT.js, then fetches and runs the client script |
| `mqttchat-main.allspeak` | Browser client (AllSpeak JS dialect) |
| `mqttchat.json` | Webson layout — header, question box with Clear/Ask, answer panel |
| `mqttchat.webmanifest`, `mqttchat-sw.js`, `icon-*.png`, `apple-touch-icon.png`, `favicon.png` | what makes it installable as an app |
| `make-icons.py` | draws those icons (Pillow; build-time only, nothing ships from it) |
| `mqttchat-server.allspeak` | Service on the PC (AllSpeak Python dialect): subscribes, dispatches, beats |
| `as_chat.py` | Plugin: access token, the Ollama stream, the GPU hand-back |
| `mqttchat.service` | User unit for the PC (`systemctl --user`, runs from this folder) |
| `credentials.php`, `chat.eclecity.net.txt.example` | Credentials endpoint for the deployed page |
| `credentials-local.example` | Credentials JSON schema (the real files are gitignored) |
| `deploy.sh`, `deploy.conf.example` | Copy the client files to the web host |
| `SETTING-UP.md` | for someone building their own copy — MQTT in plain terms, the broker, the accounts, the checklist |
| `test_as_chat.py` | Plugin tests — no model, no network, no GPU needed |

`asedit.*`, `edit.html`, `server.allspeak` and `asdoc-check.py` are the AllSpeak dev environment
(this project's editor), not part of the app.

**Building your own rather than reading this one?** Start at
[`SETTING-UP.md`](SETTING-UP.md) — it explains what each part is for and what to decide before
touching anything.

## The protocol

One topic per service, plus one per browser.

| Direction | Topic | `action` | `message` |
|---|---|---|---|
| page → service | `<topic from credentials>` | `ask` | `<access token>` newline `<question>` |
| service → page | the page's own topic — `<service topic>/reply-<id>` — from its request's `sender` | `chunk` / `done` / `error` | `CHUNK\|<text>` / `DONE\|<seconds>` / `ERROR\|<reason>` |

The token is the first line so the question may itself contain anything — newlines
included. Replies are *tagged text* rather than a field, because the browser runtime hands
a script only the `message` field of a payload, never its `action`.

Every request is answered, including refusals: a wrong token, an empty question, an
over-long question, an unreachable model and an HTTP error from Ollama all come back as
`ERROR|<reason>` and are shown in red at the top of the answer panel.

A payload is *framed*, and the receiver reassembles it: a message small enough for one chunk
travels as `!last!<total> <json>`, a larger one (the page splits at 1024 bytes) as
`!part!<n> <total> <json>` for all but the last, then `!last!<total> <json>`. A publisher that is
not AllSpeak — `mosquitto_pub`, a hand-rolled script — must frame its own, or the service logs
`ignoring unframed payload` and drops it.

Anything else that arrives on the topic — a stray publication, a plain string, a number, an
action this service does not know — is ignored and logged. The service pulls messages off
the MQTT client itself rather than having the script unpack them, because the AllSpeak side
cannot check a value's type before indexing it, and a broker topic receives junk sooner or
later.

## Running it locally

**The service** (on the machine with Ollama installed), straight out of this folder:

```
allspeak mqttchat-server.allspeak
```

It wants two files beside it, both gitignored:

- `credentials` — the JSON the browser fetches (`credentials-local.example`); without it the
  service fetches `https://chat.eclecity.net/credentials.php` at startup.
- `token` — the shared access secret (mode 600). Without one the service refuses every
  request and says so in the journal.

**The page.** It is a static file, so serve the directory and open it (a dev server is on
9090 in this workspace):

```
# → http://localhost:9090/mqttchat.html
```

On `localhost` the client reads `mqttchat-credentials.json` from this directory (gitignored;
`curl -sS https://chat.eclecity.net/credentials.php > mqttchat-credentials.json` gets the
live copy); elsewhere it fetches the deployed endpoint.

Tap **Set token** and paste the access token. It is kept in that browser's storage and the
button then hides itself: with one shared token there is nothing to choose, so it stays out of
the way until it is needed. It comes back on a device that has no token, or when the service
refuses one — which is also how a rotated token reaches a device that already had it.

## Deploying

**The page** to its own subdomain:

```
cp deploy.conf.example deploy.conf     # set DEPLOY_USER / DEPLOY_HOST / DEPLOY_PATH
./deploy.sh --infra                    # DEPLOY_DRY_RUN=1 ./deploy.sh to preview
```

`chat.eclecity.net`'s web root lives on the same DreamHost account as doclets, so
`eclecity@doclets.eclecity.net:/home/eclecity/chat.eclecity.net/` is a working target even
though sshing to `chat.eclecity.net` itself needs its host key accepted first.

`credentials.php` reads `../<host>.txt` — for `chat.eclecity.net` that is
`/home/eclecity/chat.eclecity.net.txt`, one level *above* the web root, holding:

```
{"broker": "…", "username": "chat", "password": "…", "topic": "aa:bb:cc:dd:ee:ff/chat"}
```

**The service** on the PC: it runs from this same folder, because `~/dev` is synced between
the two machines. No copying:

```
mkdir -p ~/.config/systemd/user
ln -sf ~/dev/mqttchat/mqttchat.service ~/.config/systemd/user/   # symlink, not a copy: an
systemctl --user daemon-reload                                   # edit here is what runs
systemctl --user enable --now mqttchat
loginctl enable-linger graham          # keep it running without a login, and at boot
```

After editing `mqttchat-server.allspeak` or `as_chat.py`: `systemctl --user restart mqttchat`.

## Install it as an app

The page is a PWA, so it can live on a phone's home screen: open it, then **Android** — menu →
*Install app*; **iOS** — Share → *Add to Home Screen*; **desktop Chrome** — the install icon in
the address bar. It then opens full-screen from its own icon, and the token it already holds
comes with it (same origin, so the same storage).

`mqttchat-sw.js` is deliberately thin. The three shell files are fetched from the network first
and cached only as a fallback, so a reload always picks up a new build while there *is* a
connection; `credentials.php` is never cached, and neither is anything from another origin.
Offline, the page opens and says the service is not answering — which is true, since asking
needs the broker.

Note the file's type: DreamHost serves `.webmanifest` as plain text by default, which Chrome
rejects. `deploy.sh --infra` installs a one-line `.htaccess` to fix that; without it, install
works on iOS but not on Android.

## The broker

The chat page's credentials are public — anyone who loads the page can read them — so the
page gets its own MQTT account, fenced to the chat namespace by an ACL on the broker
(`rbrheating.duckdns.org`, a Hetzner VPS running mosquitto behind nginx):

```
# /etc/mosquitto/aclfile   (referenced by /etc/mosquitto/conf.d/rbr.conf)
user rbr                     # the house account: doclets, zigbee2mqtt, rbr-cloud
topic readwrite #

user chat                    # the public page and the chat service
topic readwrite <mac>/chat/#
```

Two consequences worth remembering:

- The page's reply topic is built *under* the service topic (`<service topic>/reply-<id>`),
  because a topic outside that namespace would never be delivered.
- Rotating the `chat` password (`mosquitto_passwd -b /etc/mosquitto/passwd chat …`, then
  `systemctl reload mosquitto` and update `chat.eclecity.net.txt`) is the way to cut off a
  leaked copy of the page's credentials. Nothing else on the broker is affected.


## Settings

Read from the environment by `as_chat.py` (the systemd unit sets the first two).

| Variable | Default | Meaning |
|---|---|---|
| `MQTTCHAT_OLLAMA_URL` | `http://localhost:11434` | Where Ollama listens |
| `MQTTCHAT_MODEL` | `qwen3.5:4b` | The model to answer with (`ollama pull` it) |
| `MQTTCHAT_SYSTEM` | a short phone-answering prompt | System message sent with every question |
| `MQTTCHAT_NUM_CTX` | `8192` | Context window |
| `MQTTCHAT_TEMPERATURE` | `0.7` | Sampling temperature |
| `MQTTCHAT_MAX_PROMPT` | `4000` | Questions longer than this are refused |
| `MQTTCHAT_TIMEOUT` | `300` | Seconds to wait between stream fragments |
| `MQTTCHAT_CONNECT_TIMEOUT` | `10` | Seconds to wait for Ollama to start answering |
| `MQTTCHAT_KEEP_ALIVE` | `5m` | How long the model stays loaded after an answer (`0` = not at all) |
| `MQTTCHAT_TOKEN` | — | The access token; a `token` file or `~/.mqttchat-token` is used instead |
| `MQTTCHAT_VIDEO_PROCS` | kdenlive, melt, ffmpeg, … | Tools whose presence means the GPU is wanted elsewhere |
| `MQTTCHAT_GPU_CHECK` | `2` | Seconds between GPU probes |
| `MQTTCHAT_GPU_CLIENT_MIB` | `512` | A GPU client holding this much VRAM counts as video work |

## Sharing the GPU

The model stays loaded for `MQTTCHAT_KEEP_ALIVE` after each answer, which is what makes a
follow-up question fast. Every service-loop tick also beats, and a beat hands the GPU back
the moment something else wants it — so an edit session does not have to wait out the
keep-alive window.

Video work is spotted the same way doclets spots it: a tool named in
`MQTTCHAT_VIDEO_PROCS` that is *running* (no nvidia-smi needed, and it catches an editor
holding an OpenGL preview), or, when nvidia-smi is available, a client on the GPU whose
executable matches one of those names or that is holding at least
`MQTTCHAT_GPU_CLIENT_MIB` of VRAM. Matching is on the executable, not the whole command
line, because browser command lines carry flags that would eventually spell a short tool
name like `obs` by chance. The journal says which detector is in use.

## Tests

```
python3 test_as_chat.py                     # the plugin: streaming, tokens, GPU hand-back
python3 asdoc-check.py *.allspeak            # doc blocks: 0 errors, 0 warnings
```

The client is checked by compiling it against the real runtime bundle (the compiler catches
the mistakes that matter — `cat` placement, undeclared variables, bad labels), and by
running it headlessly in Node against stubbed DOM/MQTT/fetch: it dials the broker, renders
the layout, sends `token\nquestion` with the right `sender`, escapes and paints streamed
chunks, re-enables the button on `DONE|`, and shows an `ERROR|` reason without wedging.

## Notes and limits

- **One question at a time.** The service answers a request inside its loop, so a second
  question waits for the first answer to finish. Fine for one person; the queue is the
  broker's.
- **No conversation memory.** Each question is answered on its own, as agreed.
- **The broker credentials are not secret.** They are served to any browser that loads the
  page, because the page has to connect to the broker itself. The access token — checked on
  the PC, and never in the page — is what gates the model. If the broker credentials ever
  leak further than you like, rotate them at the broker: nothing here depends on their value.
- **The page must be served over HTTPS** for the deployed case; browsers refuse a
  `wss://` connection from an `http://` page.
