import os
import json
from fastapi import FastAPI, UploadFile, File, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import pandas as pd
from io import BytesIO
import requests
from supabase import create_client

# =======================
# ENVIRONMENT VARIABLES
# =======================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "excels")

if not all([SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY]):
    raise RuntimeError("Missing Supabase environment variables")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = FastAPI()

# =======================
# HTML PAGES
# =======================

LOGIN_HTML = f"""
<!doctype html>
<html>
<head>
  <title>Login</title>
  <script src="https://unpkg.com/@supabase/supabase-js@2"></script>
</head>
<body>
  <h2>Login</h2>
  <input id="email" placeholder="Email"><br>
  <input id="password" type="password" placeholder="Password"><br>
  <button onclick="login()">Entrar</button>
  <pre id="msg"></pre>

<script>
const supabase = window.supabase.createClient(
  "{SUPABASE_URL}",
  "{SUPABASE_ANON_KEY}"
);

async function login() {{
  const email = emailEl.value;
  const password = passwordEl.value;
  const {{ error }} = await supabase.auth.signInWithPassword({{
    email, password
  }});
  if (error) {{
    msg.textContent = error.message;
  }} else {{
    window.location.href = "/app";
  }}
}}

const emailEl = document.getElementById("email");
const passwordEl = document.getElementById("password");
const msg = document.getElementById("msg");
</script>
</body>
</html>
"""

APP_HTML = f"""
<!doctype html>
<html>
<head>
  <title>Dashboard</title>
  <script src="https://unpkg.com/@supabase/supabase-js@2"></script>
</head>
<body>
  <h2>Dashboard</h2>
  <input type="file" id="file"><br>
  <button onclick="upload()">Subir Excel</button>
  <pre id="out"></pre>

<script>
const supabase = window.supabase.createClient(
  "{SUPABASE_URL}",
  "{SUPABASE_ANON_KEY}"
);

async function getSession() {{
  const {{ data }} = await supabase.auth.getSession();
  if (!data.session) {{
    window.location.href = "/login";
  }}
  return data.session;
}}

async function upload() {{
  const session = await getSession();
  const file = document.getElementById("file").files[0];
  if (!file) return;

  const form = new FormData();
  form.append("file", file);

  const res = await fetch("/api/upload", {{
    method: "POST",
    headers: {{
      "Authorization": "Bearer " + session.access_token
    }},
    body: form
  }});

  out.textContent = JSON.stringify(await res.json(), null, 2);
}}

getSession();
</script>
</body>
</html>
"""

# =======================
# ROUTES
# =======================

@app.get("/", response_class=HTMLResponse)
def root():
    return LOGIN_HTML

@app.get("/login", response_class=HTMLResponse)
def login_page():
    return LOGIN_HTML

@app.get("/app", response_class=HTMLResponse)
def app_page():
    return APP_HTML

# =======================
# API
# =======================

def get_user(token: str):
    r = requests.get(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {token}"
        }
    )
    if r.status_code != 200:
        raise HTTPException(401, "Invalid session")
    return r.json()

@app.post("/api/upload")
async def upload_excel(
    file: UploadFile = File(...),
    authorization: str = Header(None)
):
    if not authorization:
        raise HTTPException(401, "Missing auth")

    token = authorization.split(" ")[1]
    user = get_user(token)
    user_id = user["id"]

    data = await file.read()
    df = pd.read_excel(BytesIO(data))

    if not {"Date","Open","High","Low","Close","Volume"}.issubset(df.columns):
        raise HTTPException(400, "Invalid Excel format")

    record = supabase.table("user_uploads").insert({
        "user_id": user_id,
        "file_path": "pending",
        "original_name": file.filename
    }).execute()

    upload_id = record.data[0]["id"]
    path = f"{user_id}/{upload_id}.xlsx"

    supabase.storage.from_(SUPABASE_BUCKET).upload(path, data)
    supabase.table("user_uploads").update({
        "file_path": path
    }).eq("id", upload_id).execute()

    return {"status": "ok", "rows": len(df)}
