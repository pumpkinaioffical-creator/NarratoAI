import argparse
import time
import requests


def login_or_register(session: requests.Session, host: str, username: str, password: str):
    r = session.post(f"{host}/login", data={'username': username, 'password': password}, allow_redirects=False, timeout=15)
    if r.status_code in (301, 302, 303, 307, 308):
        return

    text = r.text or ''
    if '无效' not in text and 'invalid' not in text.lower():
        return

    r2 = session.post(f"{host}/register", data={'username': username, 'password': password}, allow_redirects=False, timeout=15)
    if r2.status_code not in (200, 301, 302, 303, 307, 308):
        raise RuntimeError(f"register failed: {r2.status_code} {r2.text[:200]}")

    r3 = session.post(f"{host}/login", data={'username': username, 'password': password}, allow_redirects=False, timeout=15)
    if r3.status_code not in (301, 302, 303, 307, 308):
        raise RuntimeError(f"login failed after register: {r3.status_code} {r3.text[:200]}")


def submit_request(session: requests.Session, host: str, space_id: str, prompt: str, audio_url: str):
    deadline = time.time() + 30
    last_payload = None

    while time.time() < deadline:
        r = session.post(
            f"{host}/websockets/submit/{space_id}",
            data={'prompt': prompt, 'audio_url': audio_url},
            timeout=15,
        )
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"submit returned non-json: {r.status_code} {r.text[:200]}")

        last_payload = data

        if r.status_code == 503 and isinstance(data, dict) and data.get('error') == '远程应用未连接':
            time.sleep(1)
            continue

        if r.status_code != 200:
            raise RuntimeError(f"submit failed: {r.status_code} {data}")
        if not data.get('success'):
            raise RuntimeError(f"submit failed: {data}")
        if not data.get('request_id'):
            raise RuntimeError(f"submit missing request_id: {data}")

        return data['request_id']

    raise TimeoutError(f"submit timed out waiting for remote app connection. last={last_payload}")


def poll_status(session: requests.Session, host: str, request_id: str, timeout_sec: int = 60):
    start = time.time()
    last = None
    while time.time() - start < timeout_sec:
        r = session.get(f"{host}/websockets/status", params={'request_id': request_id}, timeout=15)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"status returned non-json: {r.status_code} {r.text[:200]}")

        last = data
        status = data.get('status')
        if status in ('completed', 'failed'):
            return data

        time.sleep(1)

    raise TimeoutError(f"timeout waiting for result. last={last}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='http://localhost:5001')
    parser.add_argument('--space-id', required=True)
    parser.add_argument('--username', default='testuser')
    parser.add_argument('--password', default='testpass')
    parser.add_argument('--audio-url', default='https://example.com/test.wav')
    parser.add_argument('--timeout', type=int, default=90)
    args = parser.parse_args()

    host = args.host.rstrip('/')
    s = requests.Session()

    login_or_register(s, host, args.username, args.password)

    req_id = submit_request(
        s,
        host,
        args.space_id,
        prompt=f'ws e2e test {time.time()}',
        audio_url=args.audio_url,
    )

    result = poll_status(s, host, req_id, timeout_sec=args.timeout)

    status = result.get('status')
    payload = result.get('result') or {}
    audio_url = None
    if isinstance(payload, dict):
        audio_url = payload.get('audio_url')

    print('request_id:', req_id)
    print('status:', status)
    print('audio_url:', audio_url)
    if isinstance(payload, dict):
        snippet = dict(payload)
        if 'image' in snippet and isinstance(snippet.get('image'), str):
            snippet['image'] = snippet['image'][:120] + '...'
        print('result_snippet:', snippet)
    else:
        print('result_snippet:', payload)

    if status != 'completed':
        raise SystemExit(2)
    if not audio_url:
        raise SystemExit(3)


if __name__ == '__main__':
    main()
