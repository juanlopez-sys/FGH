# =========================
# FGH — Stock Analyzer (Excel OHLCV)
# Backend: FastAPI (Python)
# Frontend: HTML+JS (Plotly) embebido en el mismo archivo
# Storage + Auth: Supabase
# Deploy: Render (uvicorn main:app --host 0.0.0.0 --port $PORT)
# =========================

import os
import logging
from io import BytesIO
from datetime import datetime

import numpy as np
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
# ENV VARS (Render)
# -----------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "excels")  # nombre del bucket (ej: excels)

APP_VERSION = os.getenv("APP_VERSION", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))

missing = [k for k, v in {
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY,
    "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY,
}.items() if not v]

if missing:
    raise RuntimeError(f"Missing env vars: {missing}. Configure them in Render → Environment.")

# Admin client (service role) — necesario para Storage + DB sin depender de RLS
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
  <title>Login - FGH</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://unpkg.com/@supabase/supabase-js@2"></script>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; max-width: 980px; }}
    .card {{ border: 1px solid #eee; padding: 16px; border-radius: 12px; margin-bottom: 16px; }}
    input {{ padding: 10px; width: 360px; margin: 6px 0; }}
    button {{ padding: 10px 14px; margin-top: 8px; cursor: pointer; }}
    pre {{ background:#f6f6f6; padding:12px; border-radius:8px; white-space: pre-wrap; }}
    .row {{ display:flex; gap:12px; flex-wrap:wrap; align-items:center; }}
    .muted {{ color:#555; }}
  </style>
</head>
<body>
  <h2>FGH — Login</h2>
  <div class="card">
    <div class="row">
      <input id="email" placeholder="Email" autocomplete="username">
      <input id="password" type="password" placeholder="Password" autocomplete="current-password">
    </div>
    <button id="btn" type="button">Entrar</button>
    <div class="muted" style="margin-top:8px;">Si el login responde OK pero no entra, revisa si el email está confirmado en Supabase Auth.</div>
    <pre id="msg"></pre>
  </div>

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
    show("ERROR: No se cargó supabase-js (CDN). Revisa bloqueadores/extensiones y recarga.");
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

    const {{ error }} = await sb.auth.signInWithPassword({{ email, password }});
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
        show("Login respondió OK pero no hay sesión. ¿Email confirmado en Supabase?");
        btn.disabled = false;
      }}
    }}, 500);
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
  <title>FGH — Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />

  <script src="https://unpkg.com/@supabase/supabase-js@2"></script>
  <script src="https://cdn.plot.ly/plotly-2.30.0.min.js"></script>

  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; max-width: 1200px; }}
    .topbar {{ display:flex; gap:12px; align-items:center; justify-content:space-between; flex-wrap:wrap; }}
    .card {{ border: 1px solid #eee; padding: 16px; border-radius: 12px; margin: 12px 0; }}
    button {{ padding: 10px 14px; cursor:pointer; }}
    pre {{ background:#f6f6f6; padding:12px; border-radius:8px; white-space: pre-wrap; overflow:auto; }}
    .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .muted {{ color:#555; }}
    .ok {{ color: #0a7; }}
    .bad {{ color: #c33; }}
    @media (max-width: 900px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
    .plot {{ width: 100%; height: 420px; }}
    .plotTall {{ width: 100%; height: 520px; }}
  </style>
</head>
<body>
  <div class="topbar">
    <h2 style="margin:0;">FGH — Dashboard</h2>
    <div>
      <button id="logout" type="button">Salir</button>
    </div>
  </div>

  <div class="card">
    <div class="muted">
      Sube un Excel con columnas: <b>Date, Open, High, Low, Close, Volume</b><br/>
      (Date puede venir como fecha o texto; el sistema intenta convertirlo)
    </div>
    <div style="margin-top:10px;">
      <input type="file" id="file" accept=".xlsx,.xls">
      <button id="up" type="button">Subir y analizar</button>
      <button id="loadLast" type="button">Cargar último Excel guardado</button>
    </div>
    <pre id="out">Listo.</pre>
  </div>

  <div class="grid">
    <div class="card">
      <h3 style="margin-top:0;">Resumen / Estadísticas</h3>
      <pre id="stats">Aún no hay análisis.</pre>
    </div>

    <div class="card">
      <h3 style="margin-top:0;">Estado</h3>
      <div id="status" class="muted">Esperando archivo…</div>
      <div class="muted" style="margin-top:8px;">Versión app: <span id="ver">?</span></div>
    </div>
  </div>

  <div class="card">
    <h3 style="margin-top:0;">Velas + Bandas de Bollinger + SMA/EMA</h3>
    <div id="plot_candles" class="plotTall"></div>
  </div>

  <div class="card">
    <h3 style="margin-top:0;">Volumen</h3>
    <div id="plot_volume" class="plot"></div>
  </div>

  <div class="grid">
    <div class="card">
      <h3 style="margin-top:0;">RSI</h3>
      <div id="plot_rsi" class="plot"></div>
    </div>
    <div class="card">
      <h3 style="margin-top:0;">MACD</h3>
      <div id="plot_macd" class="plot"></div>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <h3 style="margin-top:0;">Retorno acumulado</h3>
      <div id="plot_cumret" class="plot"></div>
    </div>
    <div class="card">
      <h3 style="margin-top:0;">Drawdown</h3>
      <div id="plot_dd" class="plot"></div>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <h3 style="margin-top:0;">Histograma de retornos diarios</h3>
      <div id="plot_hist" class="plot"></div>
    </div>
    <div class="card">
      <h3 style="margin-top:0;">Volatilidad (rolling)</h3>
      <div id="plot_vol" class="plot"></div>
    </div>
  </div>

<script>
  const out = document.getElementById("out");
  const statsEl = document.getElementById("stats");
  const statusEl = document.getElementById("status");
  const verEl = document.getElementById("ver");

  function logBox(t) {{
    out.textContent = t;
    console.log(t);
  }}
  function setStatus(t, ok=true) {{
    statusEl.textContent = t;
    statusEl.className = ok ? "ok" : "bad";
  }}

  if (!window.supabase || !window.supabase.createClient) {{
    logBox("ERROR: No se cargó supabase-js.");
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
    for (let i = 0; i < 10; i++) {{
      const {{ data }} = await sb.auth.getSession();
      if (data?.session) return data.session;
      await new Promise(r => setTimeout(r, 250));
    }}
    goLogin();
    return null;
  }}

  async function fetchVersion() {{
    try {{
      const res = await fetch("/version");
      const j = await res.json();
      verEl.textContent = j.version || "?";
    }} catch (e) {{
      verEl.textContent = "?";
    }}
  }}

  function prettyStats(s) {{
    try {{
      return JSON.stringify(s, null, 2);
    }} catch (e) {{
      return String(s);
    }}
  }}

  function plotAll(payload) {{
    if (!payload || !payload.series) {{
      setStatus("Respuesta sin series", false);
      return;
    }}

    const s = payload.series;
    const d = s.dates;

    // -------- Candles + overlays
    const candle = {{
      x: d, open: s.open, high: s.high, low: s.low, close: s.close,
      type: "candlestick",
      name: "OHLC"
    }};

    const sma20 = {{
      x: d, y: s.sma20, type: "scatter", mode: "lines", name: "SMA20"
    }};
    const ema20 = {{
      x: d, y: s.ema20, type: "scatter", mode: "lines", name: "EMA20"
    }};
    const bbU = {{
      x: d, y: s.bb_upper, type: "scatter", mode: "lines", name: "BB Upper"
    }};
    const bbM = {{
      x: d, y: s.bb_mid, type: "scatter", mode: "lines", name: "BB Mid"
    }};
    const bbL = {{
      x: d, y: s.bb_lower, type: "scatter", mode: "lines", name: "BB Lower"
    }};

    Plotly.newPlot("plot_candles", [candle, sma20, ema20, bbU, bbM, bbL], {{
      margin: {{ t: 30, r: 10, l: 50, b: 40 }},
      xaxis: {{ title: "Fecha" }},
      yaxis: {{ title: "Precio" }},
      showlegend: true
    }}, {{ responsive: true }});

    // -------- Volume
    const vol = {{
      x: d, y: s.volume, type: "bar", name: "Volumen"
    }};
    Plotly.newPlot("plot_volume", [vol], {{
      margin: {{ t: 30, r: 10, l: 50, b: 40 }},
      xaxis: {{ title: "Fecha" }},
      yaxis: {{ title: "Volumen" }},
      showlegend: false
    }}, {{ responsive: true }});

    // -------- RSI
    const rsi = {{
      x: d, y: s.rsi14, type: "scatter", mode: "lines", name: "RSI(14)"
    }};
    const rsiOver = {{ x: [d[0], d[d.length-1]], y: [70,70], type:"scatter", mode:"lines", name:"70" }};
    const rsiUnder= {{ x: [d[0], d[d.length-1]], y: [30,30], type:"scatter", mode:"lines", name:"30" }};
    Plotly.newPlot("plot_rsi", [rsi, rsiOver, rsiUnder], {{
      margin: {{ t: 30, r: 10, l: 50, b: 40 }},
      xaxis: {{ title: "Fecha" }},
      yaxis: {{ title: "RSI" }},
      showlegend: true
    }}, {{ responsive: true }});

    // -------- MACD
    const macd = {{ x: d, y: s.macd, type:"scatter", mode:"lines", name:"MACD" }};
    const signal = {{ x: d, y: s.macd_signal, type:"scatter", mode:"lines", name:"Signal" }};
    const hist = {{ x: d, y: s.macd_hist, type:"bar", name:"Hist" }};
    Plotly.newPlot("plot_macd", [hist, macd, signal], {{
      margin: {{ t: 30, r: 10, l: 50, b: 40 }},
      xaxis: {{ title: "Fecha" }},
      yaxis: {{ title: "MACD" }},
      showlegend: true
    }}, {{ responsive: true }});

    // -------- Cum return
    const cum = {{ x: d, y: s.cum_return, type:"scatter", mode:"lines", name:"Retorno acumulado" }};
    Plotly.newPlot("plot_cumret", [cum], {{
      margin: {{ t: 30, r: 10, l: 50, b: 40 }},
      xaxis: {{ title: "Fecha" }},
      yaxis: {{ title: "Acumulado (base 1.0)" }},
      showlegend: false
    }}, {{ responsive: true }});

    // -------- Drawdown
    const dd = {{ x: d, y: s.drawdown, type:"scatter", mode:"lines", name:"Drawdown" }};
    Plotly.newPlot("plot_dd", [dd], {{
      margin: {{ t: 30, r: 10, l: 50, b: 40 }},
      xaxis: {{ title: "Fecha" }},
      yaxis: {{ title: "Drawdown" }},
      showlegend: false
    }}, {{ responsive: true }});

    // -------- Histogram returns
    const histret = {{ x: s.returns.filter(x => x != null), type:"histogram", name:"Retornos" }};
    Plotly.newPlot("plot_hist", [histret], {{
      margin: {{ t: 30, r: 10, l: 50, b: 40 }},
      xaxis: {{ title: "Retorno diario" }},
      yaxis: {{ title: "Frecuencia" }},
      showlegend: false
    }}, {{ responsive: true }});

    // -------- Vol rolling
    const volv = {{ x: d, y: s.volatility, type:"scatter", mode:"lines", name:"Vol(20)" }};
    Plotly.newPlot("plot_vol", [volv], {{
      margin: {{ t: 30, r: 10, l: 50, b: 40 }},
      xaxis: {{ title: "Fecha" }},
      yaxis: {{ title: "Volatilidad rolling" }},
      showlegend: false
    }}, {{ responsive: true }});

    // Stats text
    statsEl.textContent = prettyStats(payload.summary);

    setStatus("Análisis listo ✅", true);
  }}

  async function uploadAndAnalyze() {{
    const session = await requireSession();
    if (!session) return;

    const file = document.getElementById("file").files[0];
    if (!file) {{
      logBox("Selecciona un Excel primero.");
      return;
    }}

    setStatus("Subiendo y analizando…", true);
    logBox("Subiendo…");

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
    logBox(text);

    if (!res.ok) {{
      setStatus("Error en upload/análisis", false);
      return;
    }}

    try {{
      const payload = JSON.parse(text);
      plotAll(payload);
    }} catch (e) {{
      setStatus("No pude parsear respuesta JSON", false);
    }}
  }}

  async function loadLast() {{
    const session = await requireSession();
    if (!session) return;

    setStatus("Cargando último Excel…", true);
    logBox("Cargando último…");

    const res = await fetch("/api/latest", {{
      method: "GET",
      headers: {{
        "Authorization": "Bearer " + session.access_token
      }}
    }});

    const text = await res.text();
    logBox(text);

    if (!res.ok) {{
      setStatus("No se pudo cargar el último Excel", false);
      return;
    }}

    try {{
      const payload = JSON.parse(text);
      plotAll(payload);
    }} catch (e) {{
      setStatus("No pude parsear JSON de /api/latest", false);
    }}
  }}

  async function logout() {{
    await sb.auth.signOut();
    goLogin();
  }}

  document.getElementById("up").addEventListener("click", uploadAndAnalyze);
  document.getElementById("loadLast").addEventListener("click", loadLast);
  document.getElementById("logout").addEventListener("click", logout);

  (async () => {{
    await requireSession();
    await fetchVersion();
  }})();
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


# =======================
# Helpers: Auth
# =======================

def get_user_from_token(token: str):
    try:
        r = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {token}",
            },
            timeout=15,
        )
    except Exception as e:
        logger.exception("Auth request failed")
        raise HTTPException(500, f"Auth request failed: {type(e).__name__}")

    if r.status_code != 200:
        raise HTTPException(401, f"Invalid session ({r.status_code}): {r.text[:200]}")
    return r.json()


# =======================
# Helpers: Storage upload (robusto)
# =======================

def storage_upload_excel(sb_admin_client, bucket: str, path: str, data: bytes, filename: str):
    # MIME según extensión (lo que queremos)
    content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if (filename or "").lower().endswith(".xls"):
        content_type = "application/vnd.ms-excel"

    # Intento 1: headers reales (muchas versiones del SDK)
    try:
        return sb_admin_client.storage.from_(bucket).upload(
            path,
            data,
            file_options={
                "content-type": content_type,
                "x-upsert": "true",
            },
        )
    except Exception as e1:
        logger.warning(f"Upload attempt 1 failed (content-type): {e1}")

    # Intento 2: camelCase (otras versiones)
    return sb_admin_client.storage.from_(bucket).upload(
        path,
        data,
        file_options={
            "contentType": content_type,
            "upsert": "true",
        },
    )


# =======================
# Helpers: Excel validation + indicators
# =======================

def validate_excel(df: pd.DataFrame):
    required = {"Date", "Open", "High", "Low", "Close", "Volume"}
    if not required.issubset(set(df.columns)):
        raise HTTPException(
            400,
            f"Invalid Excel format. Necesito {sorted(required)}. Recibí {list(df.columns)}"
        )

    # Convert Date
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    if df.empty:
        raise HTTPException(400, "La columna Date no contiene fechas válidas.")

    # Convert numeric
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    if df[["Open", "High", "Low", "Close"]].isna().any().any():
        raise HTTPException(400, "Hay valores no numéricos o vacíos en Open/High/Low/Close.")

    # Basic sanity
    if (df["High"] < df["Low"]).any():
        raise HTTPException(400, "Hay filas donde High < Low (datos inconsistentes).")

    # Sort by date
    df = df.sort_values("Date").reset_index(drop=True)

    return df


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_analysis(df: pd.DataFrame) -> dict:
    df = df.copy()

    close = df["Close"]
    df["ret"] = close.pct_change()
    df["cum_return"] = (1 + df["ret"].fillna(0)).cumprod()

    # Drawdown
    roll_max = df["cum_return"].cummax()
    df["drawdown"] = (df["cum_return"] / roll_max) - 1.0

    # SMA/EMA
    df["sma20"] = close.rolling(20).mean()
    df["ema20"] = close.ewm(span=20, adjust=False).mean()

    # Bollinger (20, 2)
    mid = df["sma20"]
    std = close.rolling(20).std()
    df["bb_mid"] = mid
    df["bb_upper"] = mid + 2 * std
    df["bb_lower"] = mid - 2 * std

    # RSI(14)
    df["rsi14"] = rsi(close, 14)

    # MACD (12,26,9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # Volatility rolling (20) on returns
    df["volatility"] = df["ret"].rolling(20).std()

    # ---------- Summary stats ----------
    rets = df["ret"].dropna()
    pos = (rets > 0).sum()
    neg = (rets < 0).sum()
    total = len(rets)

    avg_up = float(rets[rets > 0].mean()) if (rets > 0).any() else None
    avg_dn = float(rets[rets < 0].mean()) if (rets < 0).any() else None

    best_day = None
    worst_day = None
    if total > 0:
        i_best = rets.idxmax()
        i_worst = rets.idxmin()
        best_day = {"date": df.loc[i_best, "Date"].date().isoformat(), "return": float(rets.loc[i_best])}
        worst_day = {"date": df.loc[i_worst, "Date"].date().isoformat(), "return": float(rets.loc[i_worst])}

    # monthly best/worst (by month)
    dfm = df[["Date", "ret"]].dropna().copy()
    if not dfm.empty:
        dfm["month"] = dfm["Date"].dt.to_period("M").astype(str)
        monthly = dfm.groupby("month")["ret"].apply(lambda x: (1 + x).prod() - 1)
        best_month = {"month": monthly.idxmax(), "return": float(monthly.max())} if not monthly.empty else None
        worst_month = {"month": monthly.idxmin(), "return": float(monthly.min())} if not monthly.empty else None
    else:
        best_month = None
        worst_month = None

    max_dd = float(df["drawdown"].min()) if df["drawdown"].notna().any() else None

    # Sharpe / Sortino (daily -> annualized, rf=0)
    sharpe = None
    sortino = None
    if total > 10:
        mean = rets.mean()
        std = rets.std()
        if std and std > 0:
            sharpe = float((mean / std) * np.sqrt(252))

        downside = rets[rets < 0]
        dstd = downside.std()
        if dstd and dstd > 0:
            sortino = float((mean / dstd) * np.sqrt(252))

    summary = {
        "rows": int(len(df)),
        "date_range": {
            "start": df["Date"].iloc[0].date().isoformat(),
            "end": df["Date"].iloc[-1].date().isoformat(),
        },
        "positive_days_pct": float(pos / total) if total else None,
        "negative_days_pct": float(neg / total) if total else None,
        "avg_return_up_days": avg_up,
        "avg_return_down_days": avg_dn,
        "best_day": best_day,
        "worst_day": worst_day,
        "best_month": best_month,
        "worst_month": worst_month,
        "max_drawdown": max_dd,
        "sharpe_annualized": sharpe,
        "sortino_annualized": sortino,
    }

    # ---------- Series for charts ----------
    # Convert dates to ISO strings for JS
    dates = df["Date"].dt.strftime("%Y-%m-%d").tolist()

    def to_list(col):
        # convert numpy/pandas to python floats or None
        out = []
        for v in df[col].tolist():
            if pd.isna(v):
                out.append(None)
            else:
                # keep as float
                out.append(float(v))
        return out

    series = {
        "dates": dates,
        "open": to_list("Open"),
        "high": to_list("High"),
        "low": to_list("Low"),
        "close": to_list("Close"),
        "volume": to_list("Volume"),
        "returns": [None if pd.isna(v) else float(v) for v in df["ret"].tolist()],
        "cum_return": to_list("cum_return"),
        "drawdown": to_list("drawdown"),
        "sma20": to_list("sma20"),
        "ema20": to_list("ema20"),
        "bb_upper": to_list("bb_upper"),
        "bb_mid": to_list("bb_mid"),
        "bb_lower": to_list("bb_lower"),
        "rsi14": to_list("rsi14"),
        "macd": to_list("macd"),
        "macd_signal": to_list("macd_signal"),
        "macd_hist": to_list("macd_hist"),
        "volatility": to_list("volatility"),
    }

    return {"summary": summary, "series": series}


def download_from_storage(bucket: str, path: str) -> bytes:
    """
    Descarga directa usando el endpoint Storage con Authorization service_role.
    Evita depender de métodos "download()" que varían entre versiones.
    """
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{path}"
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"},
            timeout=30,
        )
    except Exception as e:
        logger.exception("Storage download request failed")
        raise HTTPException(500, f"Storage download failed: {type(e).__name__}")

    if r.status_code != 200:
        raise HTTPException(500, f"Storage download error ({r.status_code}): {r.text[:200]}")
    return r.content


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
            raise HTTPException(400, f"No pude leer el Excel: {type(e).__name__}. (Asegura 'openpyxl' en requirements)")

        df = validate_excel(df)

        # Insert metadata row (tabla debe existir)
        try:
            rec = sb_admin.table("user_uploads").insert({
                "user_id": user_id,
                "file_path": "pending",
                "original_name": file.filename or "upload.xlsx",
            }).execute()
        except Exception as e:
            logger.exception("DB insert user_uploads failed")
            raise HTTPException(500, f"DB error insert user_uploads: {type(e).__name__}. ¿Existe la tabla user_uploads?")

        if not rec.data or "id" not in rec.data[0]:
            raise HTTPException(500, f"user_uploads insert no devolvió id: {rec.data}")

        upload_id = rec.data[0]["id"]
        path = f"{user_id}/{upload_id}.xlsx"

        # Upload to storage (robusto y compatible con Restrict MIME types)
        try:
            resp = storage_upload_excel(sb_admin, SUPABASE_BUCKET, path, data, file.filename or "upload.xlsx")
            logger.info(f"Storage upload resp: {resp}")
        except Exception as e:
            logger.exception("Storage upload failed")
            raise HTTPException(500, f"Storage upload error REAL: {str(e)}")

        # Update row with file_path
        try:
            sb_admin.table("user_uploads").update({"file_path": path}).eq("id", upload_id).execute()
        except Exception as e:
            logger.exception("DB update user_uploads failed")
            raise HTTPException(500, f"DB update error: {type(e).__name__}")

        analysis = compute_analysis(df)

        return JSONResponse({
            "ok": True,
            "path": path,
            "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "note": "Archivo subido, validado y analizado correctamente.",
            **analysis,
        })

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.exception("Unexpected /api/upload error")
        raise HTTPException(500, f"Unexpected server error: {type(e).__name__}")


@app.get("/api/latest")
def latest_excel(authorization: str = Header(None)):
    try:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing Authorization: Bearer <token>")

        token = authorization.split(" ", 1)[1].strip()
        user = get_user_from_token(token)
        user_id = user["id"]

        # Busca el último upload (necesita que exista tabla user_uploads)
        try:
            q = (
                sb_admin.table("user_uploads")
                .select("id,user_id,file_path,original_name,created_at")
                .eq("user_id", user_id)
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
        except Exception as e:
            logger.exception("DB select user_uploads failed")
            raise HTTPException(500, f"DB select error: {type(e).__name__}")

        if not q.data:
            raise HTTPException(404, "No hay Excel guardado para este usuario todavía.")

        row = q.data[0]
        path = row.get("file_path")
        if not path or path == "pending":
            raise HTTPException(404, "Último registro no tiene file_path válido.")

        # Descarga el archivo y analiza
        content = download_from_storage(SUPABASE_BUCKET, path)

        try:
            df = pd.read_excel(BytesIO(content))
        except Exception as e:
            logger.exception("Error leyendo Excel descargado")
            raise HTTPException(500, f"No pude leer el Excel descargado: {type(e).__name__}")

        df = validate_excel(df)
        analysis = compute_analysis(df)

        return JSONResponse({
            "ok": True,
            "path": path,
            "original_name": row.get("original_name"),
            "note": "Último Excel cargado y analizado.",
            **analysis,
        })

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.exception("Unexpected /api/latest error")
        raise HTTPException(500, f"Unexpected server error: {type(e).__name__}")
