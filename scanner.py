
import json
import urllib.request

url = "https://api-contract.weex.com/capi/v3/market/ticker/24hr?symbol=BTCUSDT"

print("Testing WEEX API connection...")

req = urllib.request.Request(
    url,
    headers={"User-Agent": "Mozilla/5.0"}
)

with urllib.request.urlopen(req, timeout=30) as response:
    data = json.load(response)

print("WEEX CONNECTION OK")
print(json.dumps(data, indent=2))
