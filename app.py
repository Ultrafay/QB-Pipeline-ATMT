
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from contextlib import asynccontextmanager
import uvicorn
import shutil
import os
import uuid
import base64
import requests as http_requests
from pathlib import Path
import ocr_engine
from typing import Optional
from dotenv import set_key
from fastapi.middleware.cors import CORSMiddleware

# ── Drive Processor (lazy init) ──────────────────────────────

drive_processor = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start Drive watcher on startup, stop on shutdown."""
    global drive_processor
    
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if folder_id:
        try:
            from workers.drive_processor import DriveProcessor
            drive_processor = DriveProcessor()
            await drive_processor.start()
        except Exception as e:
            print(f"[App] Drive watcher failed to start: {e}")
            import traceback
            traceback.print_exc()
            drive_processor = None
    else:
        print("[App] GOOGLE_DRIVE_FOLDER_ID not set — Drive watcher disabled")
    
    yield  # App is running
    
    # Shutdown
    if drive_processor:
        await drive_processor.stop()


app = FastAPI(lifespan=lifespan)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://sandbox-quickbooks.api.intuit.com",
        "https://quickbooks.api.intuit.com",
        "https://appcenter.intuit.com",
        "https://oauth.platform.intuit.com",
        "*"  # To be restricted in real production to Vercel/Railway frontend domains
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Create directories if they don't exist
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

@app.get("/")
async def read_index():
    return JSONResponse(content={"message": "Go to /static/index.html for the UI"})

@app.get("/health")
async def health_check():
    """Health check endpoint for Railway deployment"""
    return JSONResponse(status_code=200, content={"status": "ok"})

@app.get("/launch", response_class=HTMLResponse)
async def launch_page():
    """Landing page shown after QBO authentication"""
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Launch - ATH by Solvevia</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f3f4f6; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
            .card { background: white; padding: 2.5rem; border-radius: 12px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.1); text-align: center; max-width: 450px; width: 100%; }
            h1 { color: #111827; margin-bottom: 0.5rem; font-size: 1.8rem; }
            p { color: #6b7280; margin-bottom: 2rem; }
            .status { padding: 1.25rem; border-radius: 8px; margin-bottom: 2rem; background: #f9fafb; transition: all 0.3s ease; }
            .status.connected { background: #ecfdf5; color: #065f46; border: 1px solid #a7f3d0; }
            .status.disconnected { background: #fef2f2; color: #991b1b; border: 1px solid #fecaca; }
            .btn { display: inline-block; background-color: #2563eb; color: white; padding: 0.75rem 2rem; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 1.1rem; transition: background-color 0.2s, transform 0.1s; border: none; cursor: pointer; }
            .btn:hover { background-color: #1d4ed8; }
            .btn:active { transform: scale(0.98); }
            .loader { display: inline-block; width: 20px; height: 20px; border: 3px solid rgba(0,0,0,0.1); border-radius: 50%; border-top-color: #3b82f6; animation: spin 1s ease-in-out infinite; }
            @keyframes spin { to { transform: rotate(360deg); } }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Welcome to ATH</h1>
            <p>Powered by Solvevia</p>
            
            <div id="qbo-status" class="status">
                <div class="loader"></div>
                <div style="margin-top: 0.5rem">Checking QuickBooks connection...</div>
            </div>
            
            <a href="/static/index.html" class="btn">Go to Dashboard</a>
        </div>

        <script>
            fetch('/api/qbo/status')
                .then(response => response.json())
                .then(data => {
                    const statusDiv = document.getElementById('qbo-status');
                    if (data.connected) {
                        statusDiv.className = 'status connected';
                        statusDiv.innerHTML = `✅ <strong>Connected to QuickBooks</strong><br><span style="display:inline-block; margin-top:0.5rem; color:#047857;">${data.company || ''}</span>`;
                    } else {
                        statusDiv.className = 'status disconnected';
                        statusDiv.innerHTML = '❌ <strong>Not connected to QuickBooks</strong><br><span style="display:inline-block; margin-top:0.5rem; font-size:0.9em; color:#b91c1c;">Please connect from the Dashboard settings.</span>';
                    }
                })
                .catch(error => {
                    const statusDiv = document.getElementById('qbo-status');
                    statusDiv.className = 'status disconnected';
                    statusDiv.innerHTML = '⚠️ <strong>Error checking status</strong>';
                });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.post("/api/extract")
async def extract_invoice(file: UploadFile = File(...)):
    try:
        # 1. Save uploaded file with unique ID
        file_id = str(uuid.uuid4())
        filename = f"{file_id}_{file.filename}"
        file_path = UPLOAD_DIR / filename
        
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        # 2. Run OCR Engine
        result = ocr_engine.process_invoice(file_path, file_id)
        
        return JSONResponse(content=result)
        
    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        print(error_msg)
        with open("server_error.log", "w") as f:
            f.write(error_msg)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/invoices")
async def list_invoices(status: Optional[str] = None):
    """List invoices from Google Sheets"""
    if not ocr_engine.sheets:
         raise HTTPException(status_code=503, detail="Google Sheets service not available")
    
    invoices = ocr_engine.sheets.get_invoices(status_filter=status)
    return JSONResponse(content={"invoices": invoices})

@app.post("/api/invoices/{file_id}/approve")
async def approve_invoice(file_id: str):
    """Mark invoice as approved"""
    if not ocr_engine.sheets:
         raise HTTPException(status_code=503, detail="Google Sheets service not available")
         
    success = ocr_engine.sheets.update_status(file_id, "Approved")
    if not success:
        raise HTTPException(status_code=404, detail="Invoice not found or update failed")
        
    return JSONResponse(content={"message": "Invoice approved", "file_id": file_id})

@app.post("/api/invoices/{file_id}/push-to-qb")
async def push_to_qb(file_id: str):
    """Push to QuickBooks (Stub)"""
    if not ocr_engine.sheets:
         raise HTTPException(status_code=503, detail="Google Sheets service not available")

    qb_id = f"Bill-{file_id[:8]}"
    success = ocr_engine.sheets.update_status(file_id, "Pushed to QB", qb_transaction_id=qb_id)
    
    if not success:
         raise HTTPException(status_code=404, detail="Invoice not found or update failed")

    return JSONResponse(content={"message": "Pushed to QuickBooks", "qb_id": qb_id})

@app.get("/api/drive-watcher/status")
async def drive_watcher_status():
    """Get Drive watcher status"""
    if not drive_processor:
        return JSONResponse(content={
            "is_running": False,
            "message": "Drive watcher not configured. Set GOOGLE_DRIVE_FOLDER_ID in .env"
        })
    return JSONResponse(content=drive_processor.get_status())


# ── QuickBooks OAuth Flow ─────────────────────────────────────────────────────
# Visit /api/qbo/connect in your browser to get fresh tokens whenever they expire.

_QBO_AUTH_BASE   = "https://appcenter.intuit.com/connect/oauth2"
_QBO_TOKEN_URL   = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_QBO_REVOKE_URL  = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
_QBO_SCOPES      = "com.intuit.quickbooks.accounting"
# Must match exactly what is set in your app's Redirect URIs on developer.intuit.com
_QBO_REDIRECT    = os.getenv("QBO_REDIRECT_URI", "http://localhost:8000/auth/quickbooks/callback")


@app.get("/auth/quickbooks/connect")
async def qbo_connect():
    """
    Step 1 of QBO OAuth: redirect the browser to Intuit's authorization page.
    Open /auth/quickbooks/connect in your browser to get new tokens.
    """
    client_id = os.getenv("QBO_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(status_code=500, detail="QBO_CLIENT_ID not set in .env")

    auth_url = (
        f"{_QBO_AUTH_BASE}"
        f"?client_id={client_id}"
        f"&redirect_uri={_QBO_REDIRECT}"
        f"&response_type=code"
        f"&scope={_QBO_SCOPES}"
        f"&state=qbo_oauth"
    )
    return RedirectResponse(url=auth_url)


@app.get("/auth/quickbooks/callback")
async def qbo_callback(request: Request):
    """
    Step 2 of QBO OAuth: Intuit redirects here with code + realmId.
    Exchanges the code for tokens and saves them to .env automatically.
    """
    params     = dict(request.query_params)
    code       = params.get("code")
    realm_id   = params.get("realmId")
    error      = params.get("error")

    if error:
        return JSONResponse(status_code=400, content={"error": error, "description": params.get("error_description", "")})

    if not code or not realm_id:
        return JSONResponse(status_code=400, content={"error": "Missing code or realmId from Intuit callback"})

    client_id     = os.getenv("QBO_CLIENT_ID", "")
    client_secret = os.getenv("QBO_CLIENT_SECRET", "")

    credentials = f"{client_id}:{client_secret}"
    encoded     = base64.b64encode(credentials.encode()).decode()

    try:
        resp = http_requests.post(
            _QBO_TOKEN_URL,
            headers={
                "Authorization": f"Basic {encoded}",
                "Content-Type":  "application/x-www-form-urlencoded",
                "Accept":        "application/json",
            },
            data={
                "grant_type":   "authorization_code",
                "code":         code,
                "redirect_uri": _QBO_REDIRECT,
            },
            timeout=15,
        )
        resp.raise_for_status()
        tokens = resp.json()

        access_token  = tokens["access_token"]
        refresh_token = tokens["refresh_token"]

        # Also reload in the live qbo instance if available
        if ocr_engine.qbo:
            # Reusing the newly updated token persistence logic
            ocr_engine.qbo._save_tokens(access_token, refresh_token, realm_id)
        else:
            # Fallback inline implementation if QBO service is not initialized
            railway_token = os.getenv("RAILWAY_API_TOKEN")
            service_id    = os.getenv("RAILWAY_SERVICE_ID")

            if railway_token and service_id:
                project_id = os.getenv("RAILWAY_PROJECT_ID")
                environment_id = os.getenv("RAILWAY_ENVIRONMENT_ID")
                url = "https://backboard.railway.com/graphql/v2"
                headers = {
                    "Authorization": f"Bearer {railway_token}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "query": "mutation variableCollectionUpsert($input: VariableCollectionUpsertInput!) { variableCollectionUpsert(input: $input) }",
                    "variables": {
                        "input": {
                            "projectId": project_id,
                            "environmentId": environment_id,
                            "serviceId": service_id,
                            "variables": {
                                "QBO_ACCESS_TOKEN": access_token,
                                "QBO_REFRESH_TOKEN": refresh_token,
                                "QBO_REALM_ID": realm_id
                            }
                        }
                    }
                }
                try:
                    resp = http_requests.post(url, headers=headers, json=payload, timeout=15)
                    if resp.status_code == 405:
                        http_requests.patch(url, headers=headers, json=payload, timeout=15)
                except Exception as e:
                    print(f"[QBO] Exception updating Railway vars: {e}")
            else:
                env_path = str(Path(__file__).resolve().parent / ".env")
                set_key(env_path, "QBO_ACCESS_TOKEN",  access_token)
                set_key(env_path, "QBO_REFRESH_TOKEN", refresh_token)
                set_key(env_path, "QBO_REALM_ID",      realm_id)

        return RedirectResponse(url="/launch")

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Token exchange failed: {str(e)}"})


@app.post("/auth/quickbooks/disconnect")
async def qbo_disconnect():
    """
    Revokes the QBO tokens and clears them from .env and memory.
    """
    client_id     = os.getenv("QBO_CLIENT_ID", "")
    client_secret = os.getenv("QBO_CLIENT_SECRET", "")
    refresh_token = os.getenv("QBO_REFRESH_TOKEN", "")

    if not refresh_token:
        # If we have no token, just pretend we successfully disconnected
        return JSONResponse(content={"message": "Already disconnected"})

    credentials = f"{client_id}:{client_secret}"
    encoded     = base64.b64encode(credentials.encode()).decode()

    try:
        # Call intuit revoke endpoint
        resp = http_requests.post(
            _QBO_REVOKE_URL,
            headers={
                "Authorization": f"Basic {encoded}",
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            },
            json={
                "token": refresh_token
            },
            timeout=15,
        )
        # Even if token is already expired/invalid, we want to clear locally
        
        # Clear tokens via Railway API or .env
        railway_token = os.getenv("RAILWAY_API_TOKEN")
        service_id    = os.getenv("RAILWAY_SERVICE_ID")

        if railway_token and service_id:
            project_id = os.getenv("RAILWAY_PROJECT_ID")
            environment_id = os.getenv("RAILWAY_ENVIRONMENT_ID")
            url = "https://backboard.railway.com/graphql/v2"
            headers = {"Authorization": f"Bearer {railway_token}", "Content-Type": "application/json"}
            payload = {
                "query": "mutation variableCollectionUpsert($input: VariableCollectionUpsertInput!) { variableCollectionUpsert(input: $input) }",
                "variables": {
                    "input": {
                        "projectId": project_id,
                        "environmentId": environment_id,
                        "serviceId": service_id,
                        "variables": {
                            "QBO_ACCESS_TOKEN": "",
                            "QBO_REFRESH_TOKEN": "",
                            "QBO_REALM_ID": ""
                        }
                    }
                }
            }
            try:
                resp = http_requests.post(url, headers=headers, json=payload, timeout=15)
                if resp.status_code == 405:
                    http_requests.patch(url, headers=headers, json=payload, timeout=15)
            except Exception as e:
                print(f"[QBO] Exception clearing Railway vars: {e}")
        else:
            env_path = str(Path(__file__).resolve().parent / ".env")
            set_key(env_path, "QBO_ACCESS_TOKEN",  "")
            set_key(env_path, "QBO_REFRESH_TOKEN", "")
            set_key(env_path, "QBO_REALM_ID",      "")

        # Clear from live instance
        if ocr_engine.qbo:
            ocr_engine.qbo.access_token  = ""
            ocr_engine.qbo.refresh_token = ""
            ocr_engine.qbo.realm_id      = ""
            ocr_engine.qbo.company       = "Disconnected"
            
        return JSONResponse(content={"message": "Successfully disconnected from QuickBooks"})

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Token revocation failed: {str(e)}"})


@app.get("/api/qbo/status")
async def qbo_status():
    """Check if the current QBO connection is alive."""
    if not ocr_engine.qbo:
        return JSONResponse(content={"connected": False, "reason": "QBO not initialized"})
    try:
        resp = ocr_engine.qbo._request("GET", "query", params={"query": "SELECT * FROM CompanyInfo"})
        if resp.status_code == 200:
            info = resp.json().get("QueryResponse", {}).get("CompanyInfo", [{}])[0]
            return JSONResponse(content={
                "connected": True,
                "company": info.get("CompanyName", "Unknown"),
                "realm_id": ocr_engine.qbo.realm_id,
            })
        return JSONResponse(content={"connected": False, "status_code": resp.status_code, "detail": resp.text[:200]})
    except Exception as e:
        return JSONResponse(content={"connected": False, "error": str(e)})


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)