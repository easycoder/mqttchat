#!/usr/bin/env python3
"""Tests for the chat plugin in as_chat.py.

Ollama is faked (FakeOllama replaces as_chat.requests), so these run anywhere — no model,
no network, no GPU. Run with: python3 test_as_chat.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import as_chat as mod  # noqa: E402


###############################################################################
# Fakes

class FakeStreamResponse:
    """A stand-in for an Ollama response: a context manager that streams lines."""

    def __init__(self, events=None, status=200, body=None):
        self._events = events or []
        self.status_code = status
        self._body = body if body is not None else {'done_reason': 'load'}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self):
        for event in self._events:
            yield json.dumps(event).encode('utf-8')

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise mod.requests.exceptions.HTTPError(f'{self.status_code}')


class FakeOllama:
    """Replaces as_chat.requests: records every call and scripts the reply."""

    def __init__(self, fragments=('Hello', ' world'), status=200, error_body=None,
                 failure=None, error_event=None, empty=False):
        self.exceptions = mod.requests.exceptions
        self.fragments = fragments
        self.status = status
        self.error_body = error_body or {}
        self.failure = failure          # an exception to raise instead of answering
        self.error_event = error_event  # an {'error': ...} event mid-stream
        self.empty = empty
        self.stream_calls = []
        self.residency_calls = []

    def post(self, url, json=None, timeout=None, stream=False):  # noqa: A002 - requests' API
        body = json or {}
        if stream:
            self.stream_calls.append({'url': url, 'body': body, 'timeout': timeout})
            if self.failure is not None:
                raise self.failure
            if self.status >= 400:
                return FakeStreamResponse(status=self.status, body=self.error_body)
            events = []
            if not self.empty:
                for index, fragment in enumerate(self.fragments):
                    events.append({'message': {'role': 'assistant', 'content': fragment},
                                   'done': False})
                    if self.error_event and index == 0:
                        events.append(self.error_event)
                events.append({'message': {'role': 'assistant', 'content': ''}, 'done': True})
            return FakeStreamResponse(events=events)
        self.residency_calls.append({'url': url, 'body': body})
        return FakeStreamResponse()


def make_manager(fake, **env):
    settings = {'MQTTCHAT_MODEL': 'test-model', 'MQTTCHAT_TOKEN': 'sesame'}
    settings.update(env)
    manager = mod.ChatManager(env=settings)
    mod.requests = fake
    return manager


###############################################################################
# Requests and access control

def test_split_request():
    assert mod.ChatManager.split_request('sesame\nWhat is MQTT?') == ('sesame', 'What is MQTT?')
    # only the first newline delimits: the question keeps its own line breaks
    assert mod.ChatManager.split_request('sesame\nline one\nline two') == ('sesame', 'line one\nline two')
    # no newline at all: a token with no question
    assert mod.ChatManager.split_request('sesame') == ('sesame', '')
    assert mod.ChatManager.split_request(b'sesame\nbytes work') == ('sesame', 'bytes work')
    # anything that is not text cannot be a request
    assert mod.ChatManager.split_request(None) == ('', '')
    assert mod.ChatManager.split_request({'nope': 1}) == ('', '')
    print('ok  split_request')


def test_reply_target():
    target = mod.ChatManager.reply_target
    # the browser runtime sends a topic dictionary
    assert target({'name': 'MqttChat-123', 'qos': 1}) == ('MqttChat-123', 1)
    assert target({'name': 'MqttChat-123', 'qos': '2'}) == ('MqttChat-123', 2)
    # a foreign client may send that dictionary as JSON text, or just the name
    assert target('{"name": "MqttChat-9", "qos": 1}') == ('MqttChat-9', 1)
    assert target('MqttChat-9') == ('MqttChat-9', 1)
    assert target(None) == ('', 1)
    assert target({}) == ('', 1)
    print('ok  reply_target')


def test_authorised():
    fake = FakeOllama()
    manager = make_manager(fake)
    assert manager.authorised('sesame') is True
    assert manager.authorised('sesamf') is False
    assert manager.authorised('') is False

    # no token configured means no answers, whatever the caller sends
    unconfigured = mod.ChatManager(env={'MQTTCHAT_TOKEN': ''})
    assert unconfigured.token == ''
    assert unconfigured.authorised('') is False
    assert unconfigured.authorised('sesame') is False
    print('ok  authorised')


def test_token_sources():
    fake = FakeOllama()
    home = tempfile.mkdtemp()
    cwd = os.getcwd()
    saved_home = os.environ.get('HOME')
    try:
        # the environment wins
        assert make_manager(fake, MQTTCHAT_TOKEN='from-env').token == 'from-env'

        # then a token file beside the service
        os.chdir(home)
        Path('token').write_text('from-file\n', encoding='utf-8')
        assert mod.ChatManager(env={}).token == 'from-file'

        # then the home directory
        Path('token').unlink()
        os.environ['HOME'] = home
        Path(home, '.mqttchat-token').write_text('from-home', encoding='utf-8')
        assert mod.ChatManager(env={}).token == 'from-home'
    finally:
        os.chdir(cwd)
        if saved_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = saved_home
        shutil.rmtree(home, ignore_errors=True)
    print('ok  token sources')


###############################################################################
# The model

def test_stream_chunks_in_order():
    fake = FakeOllama(fragments=('Hello', ' world', '!'))
    manager = make_manager(fake)
    seen = []
    characters, seconds = manager.stream('What is MQTT?', seen.append)

    assert seen == ['Hello', ' world', '!']
    assert characters == len('Hello world!')
    assert seconds >= 0

    call = fake.stream_calls[0]
    assert call['url'] == 'http://localhost:11434/api/chat'
    assert call['body']['model'] == 'test-model'
    assert call['body']['stream'] is True
    assert call['body']['think'] is False
    assert call['body']['messages'][1] == {'role': 'user', 'content': 'What is MQTT?'}
    assert call['body']['keep_alive'] == '5m'
    # a stalled model must not hang the service forever
    assert call['timeout'][0] == 10 and call['timeout'][1] == 300
    print('ok  stream chunks in order')


def test_stream_reports_failures():
    question = 'Anything'

    def stream_with(**kwargs):
        manager = make_manager(FakeOllama(**kwargs))
        try:
            manager.stream(question, lambda piece: None)
        except mod.ChatError as e:
            return str(e)
        raise AssertionError(f'expected a ChatError for {kwargs}')

    # an error event in the middle of the stream
    message = stream_with(error_event={'error': 'model requires more system memory'})
    assert 'system memory' in message, message

    # an HTTP-level refusal, with Ollama's own explanation
    message = stream_with(status=404, error_body={'error': "model 'test-model' not found"})
    assert 'not found' in message, message
    message = stream_with(status=500, error_body={})
    assert '500' in message, message

    # unreachable
    message = stream_with(failure=mod.requests.exceptions.ConnectionError('refused'))
    assert 'cannot reach Ollama' in message and '11434' in message, message

    # a timeout
    message = stream_with(failure=mod.requests.exceptions.ReadTimeout('slow'))
    assert 'did not answer' in message, message

    # a silent model is a failure, not an empty answer
    message = stream_with(empty=True)
    assert 'nothing' in message, message
    print('ok  stream reports failures')


###############################################################################
# The GPU hand-back

def test_beat_holds_then_expires():
    fake = FakeOllama()
    manager = make_manager(fake)
    manager.video_activity = lambda: False
    manager.run_nvidia_smi = lambda: None

    assert manager.beat() == 'idle'          # nothing has been answered yet
    manager.note_answer()
    assert manager.beat() == 'holding'       # the model is still useful
    manager._resident_until = 0.0            # the keep-alive window passes
    assert manager.beat() == 'idle'
    assert fake.residency_calls == []        # and nothing was evicted behind Ollama's back
    print('ok  beat holds then expires')


def test_beat_yields_the_gpu():
    fake = FakeOllama()
    manager = make_manager(fake)
    manager.video_activity = lambda: True
    manager.run_nvidia_smi = lambda: None
    manager.note_answer()

    assert manager.beat() == 'yielded'
    assert fake.residency_calls[0]['body'] == {'model': 'test-model', 'messages': [],
                                              'keep_alive': 0}
    # and it stays idle: the eviction is not repeated on every tick
    assert manager.beat() == 'idle'
    assert len(fake.residency_calls) == 1
    print('ok  beat yields the GPU')


def test_beat_trims_keep_alive():
    fake = FakeOllama()
    # MQTTCHAT_KEEP_ALIVE=0 means 'do not hold it at all'
    manager = make_manager(fake, MQTTCHAT_KEEP_ALIVE='0')
    manager.video_activity = lambda: False
    manager.run_nvidia_smi = lambda: None
    manager.note_answer()
    assert manager.beat() == 'idle'

    manager = make_manager(FakeOllama(), MQTTCHAT_KEEP_ALIVE='90s')
    assert manager.keep_alive_secs == 90
    assert manager.duration_seconds('5m') == 300
    assert manager.duration_seconds('1h') == 3600
    assert manager.duration_seconds('') == 300          # the default
    assert manager.duration_seconds('nonsense') == 300
    print('ok  keep-alive')


def test_gpu_busy_from_nvidia_smi():
    fake = FakeOllama()
    manager = make_manager(fake)
    manager.video_activity = lambda: False
    manager.run_nvidia_smi = lambda: (
        '1234, /usr/bin/kdenlive, 512\n'
        '2345, /opt/ollama/ollama, 4096\n'          # our own runner is ignored
        '3456, /usr/lib/firefox/firefox --flag, 40\n'  # a small client is not video work
    )
    assert manager.gpu_busy() is True
    assert 'kdenlive' in manager._gpu_reason

    # an unnamed client holding real memory counts too
    manager._gpu_checked = 0.0
    manager.run_nvidia_smi = lambda: '9999, /usr/bin/unknown, 3000\n'
    assert manager.gpu_busy() is True
    assert '3000 MiB' in manager._gpu_reason

    # and the probe is cached between beats: the GPU changes hands far more slowly than
    # the service loop comes round
    manager._gpu_checked = time.time()
    manager.run_nvidia_smi = lambda: (_ for _ in ()).throw(AssertionError('probed again'))
    manager.gpu_busy()
    manager.gpu_busy()
    print('ok  gpu busy from nvidia-smi')


def test_gpu_client_parsing():
    text = (
        '1234, /usr/bin/melt, 512 MiB\n'
        'NaN, not-a-row, 0\n'
        '5678, /usr/bin/ffmpeg, [N/A]\n'
    )
    assert mod.ChatManager.parse_gpu_clients(text) == [
        (1234, '/usr/bin/melt', 512),
        (5678, '/usr/bin/ffmpeg', 0),
    ]
    # matching must not be a substring search: browser flags carry base64 blobs
    names = mod.ChatManager.client_names('/usr/bin/firefox --token YWJzb2x1dGVseQ==')
    assert names == {'usr', 'bin', 'firefox'}
    assert 'obs' not in names
    assert mod.ChatManager.client_names('') == set()
    print('ok  gpu client parsing')


def test_video_activity_reads_proc():
    fake = FakeOllama()
    manager = make_manager(fake)
    manager.video_procs = {'definitely-not-running-now'}
    assert manager.video_activity() is False
    # the scan reads the real /proc, so a name that exists (the test runner itself)
    # stands in for a running video tool
    manager.video_procs = {Path('/proc/self/comm').read_text().strip().lower()}
    assert manager.video_activity() is True
    print('ok  video activity scan')


def main():
    # Run from an empty directory: the plugin reads its token from the working directory, and
    # a checkout that happens to hold one would quietly change what these tests exercise.
    sandbox = tempfile.mkdtemp()
    cwd = os.getcwd()
    os.chdir(sandbox)
    try:
        run_tests()
    finally:
        os.chdir(cwd)
        shutil.rmtree(sandbox, ignore_errors=True)


def run_tests():
    mod.requests = FakeOllama()  # real requests is replaced per test; this is the fallback
    test_split_request()
    test_reply_target()
    test_authorised()
    test_token_sources()
    test_stream_chunks_in_order()
    test_stream_reports_failures()
    test_beat_holds_then_expires()
    test_beat_yields_the_gpu()
    test_beat_trims_keep_alive()
    test_gpu_busy_from_nvidia_smi()
    test_gpu_client_parsing()
    test_video_activity_reads_proc()
    print('\nAll tests passed.')


if __name__ == '__main__':
    main()
