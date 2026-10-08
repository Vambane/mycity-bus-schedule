#!/usr/bin/env python3
"""Quick test script for FastAPI app."""
import uvicorn
from fastapi_app.main import app
import threading
import time
import requests

# Start server
def run_server():
    uvicorn.run(app, host='127.0.0.1', port=8001, log_level='info')

thread = threading.Thread(target=run_server, daemon=True)
thread.start()

# Wait for server
time.sleep(3)

# Test endpoints
print("Testing FastAPI endpoints...")

try:
    # Health check
    resp = requests.get('http://127.0.0.1:8001/health')
    print(f"Health check: {resp.status_code} - {resp.json()}")

    # Homepage
    resp = requests.get('http://127.0.0.1:8001/')
    print(f"Homepage: {resp.status_code} - Length: {len(resp.text)} chars")
    if resp.status_code != 200:
        print(f"Error: {resp.text[:500]}")

    # Stops API
    resp = requests.get('http://127.0.0.1:8001/api/stops')
    print(f"Stops API: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        print(f"  Found {len(data.get('stops', []))} stops")

except Exception as e:
    print(f"Error: {e}")

time.sleep(1)
