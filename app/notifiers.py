import requests

def send(config, text, payload=None):
    n=config.get('notifications',{}) or {}; payload=payload or {}
    body={'text': text, 'content': text, 'message': text, 'payload': payload}
    for url in n.get('webhook_urls') or []:
        if url: requests.post(url, json=body, timeout=8)
    if n.get('pushcut_url'):
        requests.post(n['pushcut_url'], json={'text':text, **payload}, timeout=8)
