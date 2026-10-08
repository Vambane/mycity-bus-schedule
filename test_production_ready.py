#!/usr/bin/env python3
"""
Production readiness test for FastAPI MyCiTi Timetable.
Tests all critical functionality before deployment.
"""
import uvicorn
from fastapi_app.main import app
import threading
import time
import requests
import sys

def run_server():
    uvicorn.run(app, host='127.0.0.1', port=8004, log_level='error')

print("=" * 70)
print("  FastAPI MyCiTi Timetable - Production Readiness Test")
print("=" * 70)

# Start server
thread = threading.Thread(target=run_server, daemon=True)
thread.start()
time.sleep(3)

base_url = 'http://127.0.0.1:8004'
all_passed = True

def test(name, url, checks=None, params=None):
    """Run a test and verify response."""
    global all_passed
    try:
        resp = requests.get(f"{base_url}{url}", params=params, timeout=10)

        # Basic status check
        if resp.status_code != 200:
            print(f"❌ {name}: HTTP {resp.status_code}")
            all_passed = False
            return False

        # Additional checks
        if checks:
            for check_name, check_fn in checks.items():
                if not check_fn(resp):
                    print(f"❌ {name}: {check_name} failed")
                    all_passed = False
                    return False

        print(f"✅ {name}")
        return True
    except Exception as e:
        print(f"❌ {name}: {e}")
        all_passed = False
        return False

print("\n📋 Core Functionality")
print("-" * 70)

# Test 1: Health Check
test("Health endpoint", "/health", {
    "has_status": lambda r: r.json().get("status") == "ok",
    "has_version": lambda r: "version" in r.json()
})

# Test 2: Homepage
test("Homepage loads", "/", {
    "has_html": lambda r: "<html" in r.text.lower(),
    "has_form": lambda r: "form" in r.text.lower(),
})

# Test 3: Journey Search
test("Journey search", "/", {
    "has_results": lambda r: len(r.text) > 10000,
}, params={
    'from_stop': 'Civic Centre',
    'to_stop': 'Camps Bay',
    'day_type': 'weekday'
})

# Test 4: Stop View
test("Stop timetable", "/stop/Civic%20Centre", {
    "has_content": lambda r: "Civic Centre" in r.text,
}, params={'day_type': 'weekday'})

# Test 5: System Map
test("System map page", "/map", {
    "has_iframe": lambda r: "iframe" in r.text.lower(),
})

print("\n🔌 API Endpoints")
print("-" * 70)

# Test 6: Stops API
result = test("Stops list API", "/api/stops")
if result:
    resp = requests.get(f"{base_url}/api/stops")
    stops = resp.json().get('stops', [])
    print(f"   → {len(stops)} stops available")

# Test 7: Network API
result = test("Network data API", "/api/map/network")
if result:
    resp = requests.get(f"{base_url}/api/map/network")
    data = resp.json()
    print(f"   → {len(data.get('nodes', []))} nodes, {len(data.get('routes', []))} routes")

print("\n⚡ Load Shedding Integration")
print("-" * 70)

# Test 8: Load Shedding Service
try:
    from fastapi_app.services.ls_service import get_effective_stage, stage_display_info
    stage, source = get_effective_stage()
    info = stage_display_info(stage, source)
    print(f"✅ Load shedding service")
    print(f"   → Stage {stage} ({source})")
    print(f"   → Colors: {info['bg_color']} / {info['fg_color']}")
except Exception as e:
    print(f"❌ Load shedding service: {e}")
    all_passed = False

# Test 9: Disruption Assessment
try:
    from disruption import assess_connection
    from datetime import date
    assessment = assess_connection("10:00:00", "11:00:00", 1, 2, 2, date.today())
    print(f"✅ Disruption assessment")
    print(f"   → Assessment working: affected={assessment.affected}")
except Exception as e:
    print(f"❌ Disruption assessment: {e}")
    all_passed = False

print("\n🎨 Frontend Assets")
print("-" * 70)

# Test 10: CSS
test("Main stylesheet", "/static/css/main.css", {
    "is_css": lambda r: "color:" in r.text or "background" in r.text
})

# Test 11: Components CSS
test("Components stylesheet", "/static/css/components.css", {
    "is_css": lambda r: ".card" in r.text or ".badge" in r.text
})

# Test 12: JavaScript
test("Tab switching JS", "/static/js/tabs.js", {
    "is_js": lambda r: "function" in r.text or "querySelector" in r.text
})

print("\n📊 Database")
print("-" * 70)

# Test 13: Database Connection
try:
    from fastapi_app.dependencies import get_connection
    conn = get_connection()
    result = conn.execute("SELECT COUNT(*) FROM stops").fetchone()
    print(f"✅ Database connection")
    print(f"   → {result[0]} stops in database")
except Exception as e:
    print(f"❌ Database connection: {e}")
    all_passed = False

print("\n" + "=" * 70)
if all_passed:
    print("✅ ALL TESTS PASSED - PRODUCTION READY!")
    print("=" * 70)
    print("\n🚀 Ready to deploy!")
    print("   1. Push code to GitHub")
    print("   2. Connect Render to your repo")
    print("   3. Set ESP_API_KEY environment variable (optional)")
    print("   4. Deploy!")
    print("\n📖 See DEPLOYMENT.md for detailed instructions")
    sys.exit(0)
else:
    print("❌ SOME TESTS FAILED - Review errors above")
    print("=" * 70)
    sys.exit(1)

time.sleep(1)
