#!/usr/bin/env python3
"""Debug FastAPI server to see actual errors."""
import sys
import traceback

from fastapi.responses import JSONResponse

from fastapi_app.main import app


# Add error handler to see what's going wrong
@app.exception_handler(Exception)
async def debug_exception_handler(request, exc):
    """Print the full traceback for any unhandled error, then return a 500."""
    print(f"\n=== ERROR in {request.url.path} ===", file=sys.stderr)
    print(f"Exception type: {type(exc).__name__}", file=sys.stderr)
    print(f"Exception: {exc}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    print("=" * 50, file=sys.stderr)
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "type": type(exc).__name__}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8002, log_level="info")
