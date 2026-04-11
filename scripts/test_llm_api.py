"""
Test LLM API configuration
"""
import urllib.request
import json

# Load config
with open('E:/tools/ai-bot/config/user-config.json') as f:
    config = json.load(f)

llm = config['llm']
print(f"Testing LLM API:")
print(f"  Base URL: {llm['apiBase']}")
print(f"  Model: {llm['model']}")
print(f"  API Key: {llm['apiKey'][:20]}...")
print()

# Test API call
url = f"{llm['apiBase']}/v1/chat/completions"
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {llm['apiKey']}"
}
data = {
    "model": llm['model'],
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 10
}

try:
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode('utf-8'),
        headers=headers,
        method='POST'
    )

    with urllib.request.urlopen(req, timeout=10) as response:
        result = json.loads(response.read().decode('utf-8'))
        print("[OK] API call successful!")
        print(f"Response: {json.dumps(result, indent=2)[:200]}...")

except urllib.error.HTTPError as e:
    error_body = e.read().decode('utf-8')
    print(f"[FAIL] HTTP Error {e.code}")
    print(f"Error body: {error_body[:500]}")

except Exception as e:
    print(f"[FAIL] Error: {e}")
