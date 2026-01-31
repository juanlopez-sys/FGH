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
  <input id="email" placeholder="Email" autocomplete="username"><br>
  <input id="password" type="password" placeholder="Password" autocomplete="current-password"><br>

  <!-- IMPORTANTE: type="button" -->
  <button id="btn" type="button">Entrar</button>
  <pre id="msg"></pre>

<script>
const supabase = window.supabase.createClient(
  "{SUPABASE_URL}",
  "{SUPABASE_ANON_KEY}",
  {{
    auth: {{
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: false
    }}
  }}
);

const emailEl = document.getElementById("email");
const passwordEl = document.getElementById("password");
const msg = document.getElementById("msg");
const btn = document.getElementById("btn");

function goApp() {{
  window.location.assign(window.location.origin + "/app");
}}

// Si ya hay sesión → entrar directo
(async () => {{
  const {{ data }} = await supabase.auth.getSession();
  if (data.session) goApp();
}})();

// Redirigir cuando Supabase confirme SIGNED_IN
supabase.auth.onAuthStateChange((event, session) => {{
  if (event === "SIGNED_IN" && session) {{
    goApp();
  }}
}});

async function login() {{
  msg.textContent = "";
  btn.disabled = true;
  msg.textContent = "Iniciando sesión...";

  const email = emailEl.value.trim();
  const password = passwordEl.value;

  const {{ data, error }} = await supabase.auth.signInWithPassword({{ email, password }});

  if (error) {{
    msg.textContent = "Login error: " + error.message;
    btn.disabled = false;
    return;
  }}

  // Fallback: esperar y confirmar sesión
  setTimeout(async () => {{
    const {{ data }} = await supabase.auth.getSession();
    if (data.session) {{
      goApp();
    }} else {{
      msg.textContent =
        "Se autenticó pero NO hay sesión activa.\\n" +
        "Revisa en Supabase: Auth → Users (usuario confirmado).";
      btn.disabled = false;
    }}
  }}, 400);
}}

btn.addEventListener("click", login);
passwordEl.addEventListener("keydown", (e) => {{ if (e.key === "Enter") login(); }});
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
  <button id="logout" type="button">Salir</button><br><br>

  <input type="file" id="file" accept=".xlsx,.xls"><br>
  <button id="up" type="button">Subir Excel</button>
  <pre id="out"></pre>

<script>
const supabase = window.supabase.createClient(
  "{SUPABASE_URL}",
  "{SUPABASE_ANON_KEY}",
  {{
    auth: {{
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: false
    }}
  }}
);

const out = document.getElementById("out");

function goLogin() {{
  window.location.assign(window.location.origin + "/login");
}}

// Espera corta por si la sesión tarda en persistir
async function requireSession() {{
  for (let i = 0; i < 6; i++) {{
    const {{ data }} = await supabase.auth.getSession();
    if (data.session) return data.session;
    await new Promise(r => setTimeout(r, 250));
  }}
  goLogin();
  return null;
}}

async function upload() {{
  const session = await requireSession();
  if (!session) return;

  const file = document.getElementById("file").files[0];
  if (!file) {{
    out.textContent = "Selecciona un Excel primero.";
    return;
  }}

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

async function logout() {{
  await supabase.auth.signOut();
  goLogin();
}}

document.getElementById("up").addEventListener("click", upload);
document.getElementById("logout").addEventListener("click", logout);

requireSession();
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
