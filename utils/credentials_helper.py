"""
Credentials helper for both local development and production (Railway).
- Local: reads from GOOGLE_SERVICE_ACCOUNT_JSON file path
- Production: reads GOOGLE_SERVICE_ACCOUNT_CONTENT env var, writes to temp file
"""
import os
import json
import tempfile

_temp_cred_path = None

def get_credentials_path() -> str:
    """
    Returns a file path to the Google service account credentials.
    
    Priority:
    1. GOOGLE_SERVICE_ACCOUNT_JSON env var (file path) — if the file exists
    2. GOOGLE_SERVICE_ACCOUNT_CONTENT env var (JSON string) — writes to temp file
    """
    global _temp_cred_path
    
    # Option 1: File path (local development)
    file_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "./credentials/service_account.json")
    if os.path.exists(file_path):
        print(f"[Credentials] Using local file: {file_path}")
        return file_path
    
    # Option 2: JSON content string (production)
    content = os.getenv("GOOGLE_SERVICE_ACCOUNT_CONTENT")
    if content:
        if _temp_cred_path and os.path.exists(_temp_cred_path):
            return _temp_cred_path
        
        try:
            # Validate it's valid JSON
            json.loads(content)
            
            # Write to temp file
            fd, _temp_cred_path = tempfile.mkstemp(suffix=".json", prefix="sa_creds_")
            with os.fdopen(fd, 'w') as f:
                f.write(content)
            
            print(f"[Credentials] Wrote production credentials to temp file")
            return _temp_cred_path
        except json.JSONDecodeError as e:
            raise ValueError(f"GOOGLE_SERVICE_ACCOUNT_CONTENT is not valid JSON: {e}")
    
    raise FileNotFoundError(
        "No Google credentials found. Set either:\n"
        "  - GOOGLE_SERVICE_ACCOUNT_JSON (file path) for local dev\n"
        "  - GOOGLE_SERVICE_ACCOUNT_CONTENT (JSON string) for production"
    )
