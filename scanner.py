import json
import urllib.request
import urllib.error

URL = 'https://api-contract.weex.com/capi/v3/market/ticker/24hr?symbol=BTCUSDT'
print('Testing WEEX API from GitHub Actions...', flush=True)
request = urllib.request.Request(URL, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
try:
    with urllib.request.urlopen(request, timeout=25) as response:
        data = json.load(response)
        print('HTTP status:', response.status)
        print('WEEX response:', json.dumps(data, ensure_ascii=False)[:1500])
        print('SUCCESS: GitHub Actions can access WEEX API')
except urllib.error.HTTPError as exc:
    print(f'FAILED: HTTP {exc.code} {exc.reason}')
    raise
except Exception as exc:
    print(f'FAILED: {type(exc).__name__}: {exc}')
    raise
