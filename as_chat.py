#!/usr/bin/env python3
"""
Chat plugin for AllSpeak — answers MQTT questions from a local Ollama model.

`mqttchat-server.allspeak` owns the MQTT connection; this plugin owns everything behind it:
the access token, the /api/chat stream, and the GPU hand-back.

Vocabulary:

    chat poll                                Take the next message waiting on the broker.
    chat beat                                One tick of GPU housekeeping.

Replies go to the requester's own topic as tagged text, because the browser runtime hands
a script only the `message` field of a payload, not its `action`:

    CHUNK|<text>     one streamed fragment
    DONE|<seconds>   the answer finished
    ERROR|<text>     the request was refused, or the model failed

A request is one dictionary: `action` is `ask`, `sender` names the topic to reply on, and
`message` is the access token, a newline, then the question. Anything else that arrives on
the topic — a stray publication, or a message with an action this service does not know —
is ignored and logged. The plugin pulls messages off the MQTT client itself rather than
having the script hand them over, because a broker topic receives junk sooner or later and
a script that indexes a value it cannot type-check dies on it.

Settings come from the environment (see README.md); the access token additionally from a
`token` file beside the service, or ~/.mqttchat-token.
"""

import hmac
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import requests
except Exception:  # pragma: no cover - reported when a question arrives
    requests = None

from allspeak import Handler

CHUNK_MARKER = 'CHUNK|'
DONE_MARKER = 'DONE|'
ERROR_MARKER = 'ERROR|'

DEFAULT_SYSTEM_PROMPT = (
    'You are a helpful assistant answering questions sent from the owner\'s phone. '
    'Answer directly, in plain text, without markdown formatting or preamble.'
)


class ChatError(Exception):
    """A failure worth reporting to the caller rather than logging and swallowing."""


class ChatManager:
    """Config, access control, the Ollama stream, and the GPU hand-back.

    Deliberately free of AllSpeak imports so it can be exercised directly in tests
    (see test_as_chat.py); the `Chat` handler below is the thin script-facing layer.
    """

    def __init__(self, env: Optional[Dict[str, str]] = None):
        self.env = os.environ if env is None else env
        self.ollama_url = self.setting('MQTTCHAT_OLLAMA_URL', 'http://localhost:11434').rstrip('/')
        self.model = self.setting('MQTTCHAT_MODEL', 'qwen3.5:4b')
        self.system_prompt = self.setting('MQTTCHAT_SYSTEM', DEFAULT_SYSTEM_PROMPT)
        self.num_ctx = int(self.setting('MQTTCHAT_NUM_CTX', '8192'))
        self.temperature = float(self.setting('MQTTCHAT_TEMPERATURE', '0.7'))
        self.connect_timeout = float(self.setting('MQTTCHAT_CONNECT_TIMEOUT', '10'))
        self.read_timeout = float(self.setting('MQTTCHAT_TIMEOUT', '300'))
        self.max_prompt = int(self.setting('MQTTCHAT_MAX_PROMPT', '4000'))
        # Ollama holds the model this long after an answer, which is what makes the next
        # question fast. 0 means 'let Ollama unload it immediately'.
        self.keep_alive = self.setting('MQTTCHAT_KEEP_ALIVE', '5m')
        self.keep_alive_secs = self.duration_seconds(self.keep_alive)
        self.token = self.load_token()

        # Handing the GPU back to video work (see beat).
        self.video_procs = {
            name.strip().lower() for name in self.setting(
                'MQTTCHAT_VIDEO_PROCS',
                'kdenlive,melt,ffmpeg,ffplay,vlc,mpv,obs,shotcut,olive-editor,'
                'handbrake,handbrakecli,resolve,davinci-resolve,blender'
            ).split(',') if name.strip()
        }
        self.gpu_probe_secs = float(self.setting('MQTTCHAT_GPU_CHECK', '2'))
        self.gpu_client_mib = int(self.setting('MQTTCHAT_GPU_CLIENT_MIB', '512'))
        self.own_gpu_procs = {'ollama', 'llama-server'}
        self._gpu_checked = 0.0
        self._gpu_busy = False
        self._gpu_reason = ''
        self._detector_logged = False
        self._resident_until = 0.0

    ###########################################################################
    # Config

    def setting(self, name: str, default: str) -> str:
        value = self.env.get(name)
        return default if value is None or not str(value).strip() else str(value).strip()

    def load_token(self) -> str:
        """The shared access token: environment first, then a file, else none.

        No token means no answers: a service that answers strangers because nobody
        configured it is a service that hands out the owner's GPU.
        """
        token = self.setting('MQTTCHAT_TOKEN', '')
        if token:
            return token
        for path in (Path('token'), Path.home() / '.mqttchat-token'):
            try:
                if path.is_file():
                    stored = path.read_text(encoding='utf-8').strip()
                    if stored:
                        return stored
            except OSError:
                continue
        return ''

    def describe(self) -> str:
        """A startup line for the journal — what it will do, and with what."""
        token_state = 'token loaded' if self.token else 'NO TOKEN: every request will be refused'
        return (f'{self.model} at {self.ollama_url}, keep-alive {self.keep_alive}, '
                f'{token_state}')

    ###########################################################################
    # Requests

    def authorised(self, token: str) -> bool:
        if not self.token:
            return False
        return hmac.compare_digest(str(token).encode('utf-8'), self.token.encode('utf-8'))

    @staticmethod
    def split_request(text: Any) -> Tuple[str, str]:
        """Split a request message into (token, question).

        The token is the first line, so the question may contain anything after it —
        including newlines and text that looks like JSON.
        """
        if isinstance(text, bytes):
            text = text.decode('utf-8', errors='replace')
        if not isinstance(text, str):
            return '', ''
        head, separator, rest = text.partition('\n')
        return head.strip(), (rest if separator else '')

    @staticmethod
    def reply_target(sender: Any) -> Tuple[str, int]:
        """The topic and QoS to reply on, from a request's `sender` field.

        The browser runtime sends {"name": ..., "qos": ...}; a foreign client may send the
        bare topic name or that dictionary as JSON text, so all three shapes are accepted.
        """
        if isinstance(sender, bytes):
            sender = sender.decode('utf-8', errors='replace')
        if isinstance(sender, str):
            try:
                sender = json.loads(sender)
            except ValueError:
                return sender, 1
        if isinstance(sender, dict):
            try:
                qos = int(sender.get('qos') or 1)
            except (TypeError, ValueError):
                qos = 1
            return str(sender.get('name') or ''), qos
        return '', 1

    ###########################################################################
    # The model

    def stream(self, question: str, on_chunk: Callable[[str], None]) -> Tuple[int, float]:
        """Send the question to Ollama, handing each fragment to on_chunk as it arrives.

        Returns (characters, seconds). Raises ChatError with something worth showing the
        caller — the caller is a person looking at a phone, not a log reader.
        """
        if requests is None:
            raise ChatError('the requests module is not installed on the PC')
        body = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': self.system_prompt},
                {'role': 'user', 'content': question},
            ],
            'stream': True,
            'options': {'num_ctx': self.num_ctx, 'temperature': self.temperature},
            'keep_alive': self.keep_alive,
            # Thinking models (the Qwen family) otherwise spend the reply on reasoning
            # nobody asked for. Harmless for models that do not understand the flag.
            'think': False,
        }

        started = time.time()
        characters = 0
        try:
            response = requests.post(
                f'{self.ollama_url}/api/chat',
                json=body,
                timeout=(self.connect_timeout, self.read_timeout),
                stream=True,
            )
            with response:
                if response.status_code != 200:
                    raise ChatError(self.ollama_error(response))
                for raw in response.iter_lines():
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw.decode('utf-8', errors='replace'))
                    except ValueError:
                        continue  # a partial line, or something that is not an event
                    if event.get('error'):
                        raise ChatError(str(event['error']))
                    fragment = (event.get('message') or {}).get('content') or ''
                    if fragment:
                        characters += len(fragment)
                        on_chunk(fragment)
                    if event.get('done'):
                        break
        except requests.exceptions.Timeout:
            raise ChatError(f'the model did not answer within {self.read_timeout:g}s')
        except requests.exceptions.RequestException as e:
            raise ChatError(f'cannot reach Ollama at {self.ollama_url} ({e})')

        if characters == 0:
            raise ChatError('the model returned nothing')
        return characters, time.time() - started

    @staticmethod
    def ollama_error(response: Any) -> str:
        """Ollama's own error text, when it sent one; the status code otherwise."""
        try:
            detail = response.json().get('error')
            if detail:
                return str(detail)
        except Exception:
            pass
        return f'Ollama answered {response.status_code}'

    ###########################################################################
    # Keeping the GPU available

    @staticmethod
    def duration_seconds(value: Any, default: float = 300.0) -> float:
        """Parse an Ollama-style duration ('60s', '5m', '1h', '300') as seconds."""
        text = str(value).strip().lower()
        units = {'s': 1.0, 'm': 60.0, 'h': 3600.0, 'd': 86400.0}
        try:
            if text and text[-1] in units:
                return float(text[:-1]) * units[text[-1]]
            return float(text)
        except (TypeError, ValueError):
            return default

    def note_answer(self) -> None:
        """Record that the model is resident, and for how long it will stay so."""
        self._resident_until = time.time() + max(self.keep_alive_secs, 0.0)

    def beat(self) -> str:
        """One service-loop tick: hand the GPU back if video work wants it.

        Returns 'idle' (nothing to do), 'holding' (the model is still useful) or 'yielded'.
        """
        if time.time() >= self._resident_until:
            return 'idle'  # the keep-alive window has passed; Ollama unloaded it itself
        if not self.gpu_busy():
            return 'holding'
        self._resident_until = 0.0
        if self.residency(0):
            print(f'[chat] GPU wanted by other work ({self._gpu_reason}): released {self.model}')
        return 'yielded'

    def residency(self, keep_alive: Any) -> bool:
        """Set how long one model stays resident, without generating anything.

        Ollama answers an empty-messages /api/chat with done_reason 'load', or 'unload'
        for keep_alive 0 — the cheap way to ask for the GPU back.
        """
        if requests is None:
            return False
        try:
            response = requests.post(
                f'{self.ollama_url}/api/chat',
                json={'model': self.model, 'messages': [], 'keep_alive': keep_alive},
                timeout=self.connect_timeout,
            )
            response.raise_for_status()
            response.json()
            return True
        except Exception as e:
            print(f'[chat] residency request failed for {self.model}: {e}')
            return False

    def gpu_busy(self) -> bool:
        """True if video work is on the GPU, so the model should let go of it.

        Two detectors: a listed tool merely *running* (no nvidia-smi needed, and it
        catches an editor holding an OpenGL preview, which nvidia-smi's client list does
        not report), then nvidia-smi for what is really on the GPU and how much VRAM it
        holds. Probed at most every MQTTCHAT_GPU_CHECK seconds; the answer is cached in
        between, since beats come round far more often than the GPU changes hands.
        """
        now = time.time()
        if now - self._gpu_checked < self.gpu_probe_secs:
            return self._gpu_busy
        self._gpu_checked = now

        busy = self.video_activity()
        reason = 'a video tool is running' if busy else ''

        clients = self.run_nvidia_smi()
        if clients is not None:
            for _, command, used in self.parse_gpu_clients(clients):
                names = self.client_names(command)
                if names & self.own_gpu_procs:
                    continue  # our own runner is a GPU client too, but not the competition
                tool = sorted(self.video_procs & names)
                if tool:
                    busy, reason = True, f'{tool[0]} is on the GPU'
                    break
                if self.gpu_client_mib and used >= self.gpu_client_mib:
                    busy, reason = True, f'a GPU client is holding {used} MiB of VRAM'
                    break

        self._gpu_busy, self._gpu_reason = busy, reason
        return busy

    def video_activity(self) -> bool:
        """True if a tool named in MQTTCHAT_VIDEO_PROCS is running.

        Names come from /proc/<pid>/comm, which the kernel truncates to 15 characters.
        """
        try:
            pids = os.listdir('/proc')
        except OSError:
            return False
        for pid in pids:
            if not pid.isdigit():
                continue
            try:
                with open(f'/proc/{pid}/comm') as handle:
                    if handle.read().strip().lower() in self.video_procs:
                        return True
            except OSError:
                continue
        return False

    @staticmethod
    def parse_gpu_clients(text: str) -> List[Tuple[int, str, int]]:
        """Parse `nvidia-smi --query-compute-apps=pid,process_name,used_memory`.

        Returns (pid, lowercased process name, MiB) per client; rows whose fields
        nvidia-smi cannot supply yield 0 MiB rather than an error.
        """
        clients = []
        for line in text.splitlines():
            parts = [part.strip() for part in line.split(',')]
            if len(parts) < 2 or not parts[0].isdigit():
                continue
            used = 0
            if len(parts) > 2:
                try:
                    used = int(float(parts[2].split()[0]))
                except (ValueError, IndexError):
                    used = 0
            clients.append((int(parts[0]), parts[1].lower(), used))
        return clients

    @staticmethod
    def client_names(command_line: str) -> set:
        """Names a GPU client might answer to: its executable and that path's parts.

        nvidia-smi reports a whole command line for many apps, so this must not be a
        substring search — browser flags include base64 blobs that would eventually
        spell a short tool name like 'obs'.
        """
        tokens = command_line.split()
        if not tokens:
            return set()
        return {part.lower() for part in tokens[0].split('/') if part}

    def run_nvidia_smi(self) -> Optional[str]:
        """nvidia-smi's GPU client list, or None when it cannot be queried."""
        try:
            result = subprocess.run(
                ['nvidia-smi', '--query-compute-apps=pid,process_name,used_memory',
                 '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as e:
            self.log_detector(False, e)
            return None
        if result.returncode != 0:
            lines = (result.stderr or result.stdout or '').strip().splitlines()
            self.log_detector(False, lines[0] if lines else 'nvidia-smi failed')
            return None
        self.log_detector(True)
        return result.stdout

    def log_detector(self, working: bool, detail: Any = None) -> None:
        """Say once which detector is in use, so the journal explains itself."""
        if self._detector_logged:
            return
        self._detector_logged = True
        if working:
            print('[chat] GPU detection: nvidia-smi')
        else:
            print(f'[chat] GPU detection: nvidia-smi unavailable ({detail}); '
                  f'falling back to process names')


class Chat(Handler):
    """The script-facing half: `chat poll` and `chat beat`."""

    def __init__(self, compiler):
        super().__init__(compiler)
        self.spoke = None
        self.manager = ChatManager()
        self._last_message = None
        print(f'[chat] {self.manager.describe()}')

    def getName(self):
        return 'chat'

    ###########################################################################
    # Compile

    def k_chat(self, command):
        mode = self.peek()
        if mode in ('poll', 'beat'):
            self.nextToken()
            command['mode'] = mode
            self.add(command)
            return True
        return False

    ###########################################################################
    # Values

    def modifyValue(self, value):
        """The value hook every domain must answer: this plugin adds no value types."""
        return value

    ###########################################################################
    # Run

    def r_chat(self, command):
        if command['mode'] == 'beat':
            self.manager.beat()
        else:
            self.poll()
        return self.nextPC()

    def poll(self) -> None:
        """Answer the next message waiting on the broker, if there is one.

        Messages are pulled here rather than handed over by the script because a broker topic
        receives junk sooner or later — a plain string, a number, an array — and the script
        cannot test a value's type before indexing it. The client returns the last message
        again once its own queue is empty, so identity is checked to avoid answering twice.
        """
        client = getattr(self.program, 'mqttClient', None)
        if client is None:
            return
        message = client.getReceivedMessage()
        if message is None or message is self._last_message:
            return
        self._last_message = message
        self.handle_message(message)

    def handle_message(self, message: Any) -> None:
        """Dispatch one message: answer it if it is a question, ignore it otherwise."""
        if not isinstance(message, dict):
            print('[chat] ignored a message that is not a request')
            return

        action = str(message.get('action') or '')
        if action != 'ask':
            print(f'[chat] ignored a message with action {action or "(none)"}')
            return

        self.answer(message)

    def answer(self, request: Dict[str, Any]) -> None:
        """Answer one question: check the token, then stream the reply to whoever asked."""
        token, question = ChatManager.split_request(request.get('message', ''))
        reply_topic, qos = ChatManager.reply_target(request.get('sender'))
        if not reply_topic:
            print('[chat] request carries no sender topic; cannot reply')
            return

        if not self.manager.authorised(token):
            print(f'[chat] refused a request for {reply_topic}: access token missing or wrong')
            self.reply(reply_topic, qos, 'error', ERROR_MARKER + 'access token refused')
            return

        if not question.strip():
            self.reply(reply_topic, qos, 'error', ERROR_MARKER + 'empty question')
            return

        if len(question) > self.manager.max_prompt:
            self.reply(reply_topic, qos, 'error',
                       ERROR_MARKER + f'question is longer than {self.manager.max_prompt} characters')
            return

        print(f'[chat] answering {len(question)} characters for {reply_topic}')
        try:
            characters, seconds = self.manager.stream(
                question,
                lambda fragment: self.reply(reply_topic, qos, 'chunk', CHUNK_MARKER + fragment),
            )
        except ChatError as e:
            print(f'[chat] failed: {e}')
            self.reply(reply_topic, qos, 'error', ERROR_MARKER + str(e))
            return

        print(f'[chat] answered {characters} characters in {seconds:.1f}s')
        self.manager.note_answer()
        self.reply(reply_topic, qos, 'done', f'{DONE_MARKER}{seconds:.1f}s')

    def reply(self, topic: str, qos: int, action: str, text: str) -> None:
        """Publish one tagged reply on the requester's own topic."""
        client = getattr(self.program, 'mqttClient', None)
        if client is None:
            print('[chat] no MQTT client yet: reply dropped')
            return
        payload = {'sender': None, 'action': action, 'message': text}
        client.sendMessage(topic, json.dumps(payload), qos, chunk_size=0)
