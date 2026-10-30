#!/usr/bin/env python3
"""Minimal template rendering test."""
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

# Setup templates
templates_dir = Path("fastapi_app/templates")
jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=True, cache_size=0)
templates = Jinja2Templates(env=jinja_env)

# Create a mock request
class MockRequest:  # pylint: disable=too-few-public-methods
    """Just enough of a Request for template rendering."""

    url = type('obj', (object,), {'path': '/'})

request = MockRequest()

# Try to render
try:
    response = templates.TemplateResponse(
        "pages/index.html",
        {
            "request": request,
            "page": "search",
            "all_stops": ["Stop A", "Stop B"],
            "from_stop": "",
            "to_stop": "",
            "day_type": "weekday",
            "results": None,
        }
    )
    print("SUCCESS! Template rendered")
    print(f"Body length: {len(response.body)} bytes")
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()
