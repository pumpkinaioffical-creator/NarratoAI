import argparse
import threading
import time
import tempfile
import struct
import wave

import requests
import socketio
from flask import Flask, jsonify, request


def _make_silence_wav(path: str, duration_sec: float = 1.0, sample_rate: int = 16000):
    frames = int(duration_sec * sample_rate)
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        silence_frame = struct.pack('<h', 0)
        wf.writeframes(silence_frame * frames)


class FakeIndexTTSRemote:
    def __init__(self, server_url: str, space_name: str, verbose: bool = False):
        self.server_url = server_url.rstrip('/')
        self.space_name = space_name
        self.verbose = verbose

        self.sio = socketio.Client(
            reconnection=True,
            reconnection_delay=1,
            reconnection_delay_max=5,
            logger=verbose,
            engineio_logger=verbose,
        )

        self.connected = False
        self.registered = False
        self.last_register_response = None
        self.last_error = None

        self._setup_handlers()

    def _setup_handlers(self):
        @self.sio.event
        def connect():
            self.connected = True
            self.sio.emit('register', {'space_name': self.space_name})

        @self.sio.on('register_response')
        def on_register_response(data):
            self.last_register_response = data
            self.registered = bool(data.get('success'))
            if not self.registered:
                self.last_error = data.get('message')

        @self.sio.on('inference_request')
        def on_inference_request(data):
            thread = threading.Thread(target=self._handle_inference_request, args=(data,), daemon=True)
            thread.start()

        @self.sio.event
        def disconnect():
            self.connected = False
            self.registered = False

    def connect(self):
        self.sio.connect(self.server_url, transports=['websocket', 'polling'])

    def _handle_inference_request(self, data):
        request_id = data.get('request_id')
        username = data.get('username')
        upload = data.get('upload')
        if not request_id:
            return

        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
            tmp.close()
            _make_silence_wav(tmp.name, duration_sec=1.0)

            audio_url = None

            if isinstance(upload, dict) and upload.get('put_url') and upload.get('final_url'):
                put_url = upload.get('put_url')
                final_url = upload.get('final_url')
                content_type = upload.get('content_type') or 'audio/wav'

                with open(tmp.name, 'rb') as f:
                    data_bytes = f.read()

                put_resp = requests.put(
                    put_url,
                    data=data_bytes,
                    headers={
                        'Content-Type': content_type,
                        'Content-Length': str(len(data_bytes)),
                    },
                    timeout=120,
                )

                if 200 <= put_resp.status_code < 300:
                    audio_url = final_url
                else:
                    self.last_error = f"presigned put failed: {put_resp.status_code} {put_resp.text[:200]}"

            if not audio_url:
                upload_url = f"{self.server_url}/api/upload"
                with open(tmp.name, 'rb') as f:
                    files = {'file': (f"result_{request_id[:8]}.wav", f, 'audio/wav')}
                    form = {
                        'request_id': request_id,
                        'username': username or '',
                        'folder': 'ws_results'
                    }
                    resp = requests.post(upload_url, files=files, data=form, timeout=60)

                try:
                    payload = resp.json()
                except ValueError:
                    payload = {'success': False, 'error': resp.text}

                if resp.status_code != 200 or not payload.get('success'):
                    raise RuntimeError(f"upload failed: {resp.status_code} {payload}")

                audio_url = payload.get('url') or payload.get('file_url')
                if not audio_url:
                    raise RuntimeError(f"upload response missing url: {payload}")

            self.sio.emit(
                'inference_result',
                {
                    'request_id': request_id,
                    'status': 'completed',
                    'result': {
                        'audio_url': audio_url,
                        'duration': 1.0,
                        'engine': 'fake-indextts'
                    }
                }
            )
        except Exception as e:
            self.last_error = str(e)
            self.sio.emit(
                'inference_result',
                {
                    'request_id': request_id,
                    'status': 'failed',
                    'result': {'error': str(e), 'engine': 'fake-indextts'}
                }
            )


def _submit_test_request(server_url: str, space_id: str, login_username: str, login_password: str, audio_url: str):
    s = requests.Session()
    r = s.post(f"{server_url.rstrip('/')}/login", data={'username': login_username, 'password': login_password}, timeout=30)
    if r.status_code not in (200, 302):
        raise RuntimeError(f"login failed: {r.status_code} {r.text[:200]}")

    form = {
        'prompt': f'fake indextts test {time.time()}',
        'audio_url': audio_url
    }
    r2 = s.post(f"{server_url.rstrip('/')}/websockets/submit/{space_id}", data=form, timeout=30)
    try:
        data = r2.json()
    except ValueError:
        raise RuntimeError(f"submit failed: {r2.status_code} {r2.text[:200]}")

    if r2.status_code != 200 or not data.get('success'):
        raise RuntimeError(f"submit failed: {r2.status_code} {data}")

    return data.get('request_id')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--server-url', default='http://localhost:5001')
    parser.add_argument('--space-name', required=True)
    parser.add_argument('--http-port', type=int, default=6006)
    parser.add_argument('--no-http', action='store_true')
    parser.add_argument('--verbose', action='store_true')

    parser.add_argument('--space-id', default='')
    parser.add_argument('--login-username', default='')
    parser.add_argument('--login-password', default='')
    parser.add_argument('--test-audio-url', default='https://example.com/test.wav')
    parser.add_argument('--test-on-start', action='store_true')

    args = parser.parse_args()

    remote = FakeIndexTTSRemote(args.server_url, args.space_name, verbose=args.verbose)

    def connect_bg():
        while True:
            try:
                remote.connect()
                remote.sio.wait()
            except Exception as e:
                remote.last_error = str(e)
                time.sleep(2)

    threading.Thread(target=connect_bg, daemon=True).start()

    if args.no_http:
        while True:
            time.sleep(1)

    app = Flask(__name__)

    @app.get('/health')
    def health():
        return jsonify({'ok': True})

    @app.get('/status')
    def status():
        return jsonify(
            {
                'server_url': args.server_url,
                'space_name': args.space_name,
                'connected': remote.connected,
                'registered': remote.registered,
                'last_register_response': remote.last_register_response,
                'last_error': remote.last_error,
            }
        )

    @app.post('/test')
    def test():
        body = request.get_json(silent=True) or {}
        space_id = body.get('space_id') or args.space_id
        login_username = body.get('login_username') or args.login_username
        login_password = body.get('login_password') or args.login_password
        audio_url = body.get('audio_url') or args.test_audio_url

        if not space_id:
            return jsonify({'success': False, 'error': 'missing space_id'}), 400
        if not login_username or not login_password:
            return jsonify({'success': False, 'error': 'missing login_username/login_password'}), 400

        req_id = _submit_test_request(args.server_url, space_id, login_username, login_password, audio_url)
        return jsonify({'success': True, 'request_id': req_id})

    if args.test_on_start:
        def start_test_bg():
            start = time.time()
            while time.time() - start < 20:
                if remote.registered:
                    break
                time.sleep(0.5)

            if args.space_id and args.login_username and args.login_password:
                try:
                    _submit_test_request(args.server_url, args.space_id, args.login_username, args.login_password, args.test_audio_url)
                except Exception as e:
                    remote.last_error = str(e)

        threading.Thread(target=start_test_bg, daemon=True).start()

    app.run(host='0.0.0.0', port=args.http_port, debug=False)


if __name__ == '__main__':
    main()
