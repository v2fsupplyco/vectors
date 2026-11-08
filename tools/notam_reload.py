"""Full NOTAM reload for the Vectors NOTAM server (run by .github/workflows/monitor.yml).

Downloads the NMS-API Initial Load (one bulk pull; the NMS-API allows at most one per 24 hours)
and writes reload.sql for `wrangler d1 execute`. Refuses to run if the server's last full load
was under 24 hours ago. Credentials come from GitHub Actions secrets via the environment.
"""
import base64, json, os, sys, time, urllib.request

NMS = 'https://api-nms.aim.faa.gov'
HEALTH = 'https://vectors-notams.v2fsupply.workers.dev/health'
UA = {'User-Agent': 'Vectors NOTAM reload (support@v2fsupply.com)'}

def main():
    health = json.load(urllib.request.urlopen(urllib.request.Request(HEALTH, headers=UA), timeout=30))
    last_full = health.get('lastFullLoad')
    if last_full:
        from datetime import datetime, timezone
        age = time.time() - datetime.fromisoformat(last_full.replace('Z', '+00:00')).timestamp()
        if age < 24.5 * 3600:
            print(f'Last full load was {age/3600:.1f} h ago; the NMS-API allows one bulk pull per 24 h. Skipping.')
            return 3
    key, secret = os.environ['NMS_CLIENT_ID'], os.environ['NMS_CLIENT_SECRET']
    auth = base64.b64encode(f'{key}:{secret}'.encode()).decode()
    token = json.load(urllib.request.urlopen(urllib.request.Request(
        NMS + '/v1/auth/token', data=b'grant_type=client_credentials',
        headers={**UA, 'Authorization': 'Basic ' + auth, 'Content-Type': 'application/x-www-form-urlencoded'}), timeout=30))['access_token']

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k): return None
    try:
        body = urllib.request.build_opener(NoRedirect).open(urllib.request.Request(
            NMS + '/nmsapi/v1/notams/il', headers={**UA, 'Authorization': 'Bearer ' + token}), timeout=60).read()
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 307, 308): raise
        body = e.read()
    url = json.loads(body)['data']['url']
    full = url if url.startswith('http') else NMS + url
    with urllib.request.urlopen(urllib.request.Request(full, headers={**UA, 'Authorization': 'Bearer ' + token}), timeout=300) as r, open('il.gz', 'wb') as f:
        while chunk := r.read(1 << 20): f.write(chunk)
    print('Initial Load downloaded:', os.path.getsize('il.gz'), 'bytes')
    sys.argv = ['notam_bootstrap.py', 'il.gz', 'reload.sql']
    import notam_bootstrap
    notam_bootstrap.main('il.gz', 'reload.sql')
    return 0

if __name__ == '__main__':
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
