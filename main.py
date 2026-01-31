import os
import logging
from io import BytesIO
from datetime import datetime

import pandas as pd
import requests
from fastapi import FastAPI, UploadFile, File, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from supabase import create_client

# -----------------------
# Logging (Render logs)
# -----------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FGH")

# -----------------------
# ENV VARS
# -----------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "excels")

APP_VERSION = os.getenv("APP_VERSION", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))

missing = [k for k, v in {
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY,
    "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY,
}.items() if not v]

if missing:
    raise RuntimeError(f"Missing env vars: {missing}. Configure them in Render → Settings → Environment.")

# Admin client (service role)
sb_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = FastAPI()


# =======================
# HTML (frontend embedded)
# =======================

LOGIN_HTML = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Login</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://unpkg.com/@supabase/supabase-js@2"></script>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; max-width: 820px; }}
    input {{ padding: 10px; width: 360px; margin: 6px 0; }}
    button {{ padding: 10px 14px; margin-top: 8px; }}
    pre {{ background:#f6f6f6; padding:12px; border-radius:8px; white-space: pre-wrap; }}
    .row {{ display:flex; gap:12px; flex-wrap:wrap; align-items:center; }}
  </style>
</head>
<body>
  <h2>Login</h2>
  <div class="row">
    <input id="email" placeholder="Email" autocomplete="username">
    <input id="password" type="password" placeholder="Password" autocomplete="current-password">
  </div>
  <button id="btn" type="button">Entrar</button>
  <pre id="msg"></pre>

<script>
  const msg = document.getElementById("msg");
  const btn = document.getElementById("btn");
  const emailEl = document.getElementById("email");
  const passEl = document.getElementById("password");

  function show(t) {{
    msg.textContent = t;
    console.log(t);
  }}

  if (!window.supabase || !window.supabase.createClient) {{
    show("ERROR: No se cargó supabase-js (CDN). Revisa extensiones/bloqueadores y recarga.");
    throw new Error("supabase-js not loaded");
  }}

  // IMPORTANTE: usar 'sb' para no chocar con window.supabase
  const sb = window.supabase.createClient(
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

  function goApp() {{
    window.location.assign(window.location.origin + "/app");
  }}

  // Si ya hay sesión, entrar directo
  (async () => {{
    const {{ data, error }} = await sb.auth.getSession();
    if (error) show("getSession error: " + error.message);
    if (data?.session) {{
      show("Sesión activa. Redirigiendo a /app ...");
      goApp();
    }} else {{
      show("Ingresa credenciales.");
    }}
  }})();

  sb.auth.onAuthStateChange((event, session) => {{
    console.log("Auth event:", event);
    if (event === "SIGNED_IN" && session) {{
      show("Login OK. Redirigiendo...");
      goApp();
    }}
  }});

  async function login() {{
    show("Iniciando sesión...");
    btn.disabled = true;

    const email = emailEl.value.trim();
    const password = passEl.value;

    if (!email || !password) {{
      show("Falta email o password.");
      btn.disabled = false;
      return;
    }}

    const {{ data, error }} = await sb.auth.signInWithPassword({{ email, password }});
    if (error) {{
      show("Login error: " + error.message);
      btn.disabled = false;
      return;
    }}

    // Fallback
    setTimeout(async () => {{
      const {{ data }} = await sb.auth.getSession();
      if (data?.session) {{
        goApp();
      }} else {{
        show("Login respondió OK pero no hay sesión. Revisa si el email está confirmado en Supabase.");
        btn.disabled = false;
      }}
    }}, 400);
  }}

  btn.addEventListener("click", login);
  passEl.addEventListener("keydown", (e) => {{ if (e.key === "Enter") login(); }});
</script>
</body>
</html>
"""


APP_HTML = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://unpkg.com/@supabase/supabase-js@2"></script>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; max-width: 900px; }}
    button {{ padding: 10px 14px; margin: 6px 0; }}
    pre {{ background:#f6f6f6; padding:12px; border-radius:8px; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <h2>Dashboard</h2>
  <div>
    <button id="logout" type="button">Salir</button>
  </div>

  <p>Sube un Excel con columnas: <b>Date, Open, High, Low, Close, Volume</b></p>

  <input type="file" id="file" accept=".xlsx,.xls"><br>
  <button id="up" type="button">Subir Excel</button>

  <pre id="out">Listo.</pre>

<script>
  const out = document.getElementById("out");

  function log(t) {{
    out.textContent = t;
    console.log(t);
  }}

  if (!window.supabase || !window.supabase.createClient) {{
    log("ERROR: No se cargó supabase-js.");
    throw new Error("supabase-js not loaded");
  }}

  const sb = window.supabase.createClient(
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

  function goLogin() {{
    window.location.assign(window.location.origin + "/login");
  }}

  async function requireSession() {{
    // esperar un poco por persistencia de sesión
    for (let i = 0; i < 8; i++) {{
      const {{ data }} = await sb.auth.getSession();
      if (data?.session) return data.session;
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
      log("Selecciona un Excel primero.");
      return;
    }}

    log("Subiendo y analizando...");

    const form = new FormData();
    form.append("file", file);

    const res = await fetch("/api/upload", {{
      method: "POST",
      headers: {{
        "Authorization": "Bearer " + session.access_token
      }},
      body: form
    }});

    const text = await res.text();
    log(text);
  }}

  async function logout() {{
    await sb.auth.signOut();
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
# Routes
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

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/version")
def version():
    return {"version": APP_VERSION}
    
@app.get("/debug_storage")
def debug_storage():
    try:
        r = requests.get(
            f"{SUPABASE_URL}/storage/v1/bucket",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
            timeout=20
        )
        return {
            "status_code": r.status_code,
            "text": r.text[:1000],
            "using_bucket": SUPABASE_BUCKET,
        }
    except Exception as e:
        return {"error": str(e)}


# =======================
# Helpers
# =======================

def get_user_from_token(token: str):
    try:
        r = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {token}"
            },
            timeout=15
        )
    except Exception as e:
        logger.exception("Auth request failed")
        raise HTTPException(500, f"Auth request failed: {type(e).__name__}")

    if r.status_code != 200:
        raise HTTPException(401, f"Invalid session ({r.status_code}): {r.text[:200]}")
    return r.json()


def validate_excel(df: pd.DataFrame):
    required = {"Date", "Open", "High", "Low", "Close", "Volume"}
    if not required.issubset(set(df.columns)):
        raise HTTPException(
            400,
            f"Invalid Excel format. Necesito {sorted(required)}. Recibí {list(df.columns)}"
        )

    # Date to datetime (tolerante)
    try:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    except Exception:
        raise HTTPException(400, "No pude convertir Date a fecha (datetime).")

    if df["Date"].isna().all():
        raise HTTPException(400, "La columna Date no contiene fechas válidas.")

    # Numeric columns
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    if df[["Open", "High", "Low", "Close"]].isna().any().any():
        raise HTTPException(400, "Hay valores no numéricos o vacíos en Open/High/Low/Close.")

    # Basic sanity
    if (df["High"] < df["Low"]).any():
        raise HTTPException(400, "Hay filas donde High < Low (datos inconsistentes).")

    return df


# =======================
# API
# =======================

@app.post("/api/upload")
async def upload_excel(file: UploadFile = File(...), authorization: str = Header(None)):
    try:
        # Auth
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing Authorization: Bearer <token>")

        token = authorization.split(" ", 1)[1].strip()
        user = get_user_from_token(token)
        user_id = user["id"]

        # Read file bytes
        data = await file.read()
        if not data or len(data) < 50:
            raise HTTPException(400, "Archivo vacío o inválido")

        # Read Excel
        try:
            df = pd.read_excel(BytesIO(data))
        except Exception as e:
            logger.exception("Error leyendo Excel")
            raise HTTPException(400, f"No pude leer el Excel: {type(e).__name__}. ¿Incluiste openpyxl en requirements?")

        df = validate_excel(df)

        # Insert metadata row (tabla debe existir)
        try:
            rec = sb_admin.table("user_uploads").insert({
                "user_id": user_id,
                "file_path": "pending",
                "original_name": file.filename or "upload.xlsx"
            }).execute()
        except Exception as e:
            logger.exception("DB insert user_uploads failed")
            raise HTTPException(500, f"DB error insert user_uploads: {type(e).__name__}. ¿Existe la tabla user_uploads?")

        if not rec.data or "id" not in rec.data[0]:
            raise HTTPException(500, f"user_uploads insert no devolvió id: {rec.data}")

        upload_id = rec.data[0]["id"]
        path = f"{user_id}/{upload_id}.xlsx"

        # Upload to storage (bucket debe existir)
        try:
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if (file.filename or "").lower().endswith(".xls"):
                content_type = "application/vnd.ms-excel"
                
            resp = sb_admin.storage.from_(SUPABASE_BUCKET).upload(
                path,
                data,
                file_options={
                    "contentType": content_type,
                    "upsert": True
                }
            )

            logger.info(f"Storage upload resp: {resp}")
        except Exception as e:
            logger.exception("Storage upload failed (detail)")
            raise HTTPException(500, f"Storage upload error REAL: {str(e)}")

        # Update row with file_path
        try:
            sb_admin.table("user_uploads").update({"file_path": path}).eq("id", upload_id).execute()
        except Exception as e:
            logger.exception("DB update user_uploads failed")
            raise HTTPException(500, f"DB update error: {type(e).__name__}")

        # Return OK with details
        return JSONResponse({
            "ok": True,
            "rows": int(len(df)),
            "path": path,
            "note": "Archivo subido y validado correctamente."
        })

    except HTTPException as he:
        # Mensaje claro al frontend
        raise he
    except Exception as e:
        logger.exception("Unexpected /api/upload error")
        raise HTTPException(500, f"Unexpected server error: {type(e).__name__}")
