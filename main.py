import httpx
import json
import os
import argparse
import pathlib
import psycopg2
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = pathlib.Path(__file__).resolve().parent

# ─────────────────────────────────────────
# CONFIGURAÇÃO DOS CLIENTES
# ─────────────────────────────────────────
with open(BASE_DIR / "clients.json", encoding="utf-8") as f:
    CLIENTS = json.load(f)

for client in CLIENTS:
    client["tokens"] = [os.getenv(env) for env in client["token_envs"]]

# ─────────────────────────────────────────
# CONFIGURAÇÃO
# ─────────────────────────────────────────
DOMANI_BASE     = os.getenv("DOMANI_BASE_URL")
DB_URL          = os.getenv("SUPABASE_DB_URL")
REPORT_PASSWORD = os.getenv("REPORT_PASSWORD", "domani2026")


# ─────────────────────────────────────────
# BANCO DE DADOS
# ─────────────────────────────────────────
def get_conn():
    if not DB_URL:
        raise RuntimeError("SUPABASE_DB_URL não configurada.")
    return psycopg2.connect(DB_URL)


def save_reading(client_name: str, used: int, limit_total: int):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO contact_readings (client_name, read_at, used, limit_total)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (client_name, datetime.now(timezone.utc), used, limit_total),
                )
    except Exception as e:
        print(f"[WARN] Não foi possível salvar histórico de '{client_name}': {e}")


def load_history(client_name: str, days: int = 30) -> pd.DataFrame:
    query = """
        SELECT read_at, used
        FROM contact_readings
        WHERE client_name = %s
          AND read_at >= NOW() - (%s * INTERVAL '1 day')
        ORDER BY read_at ASC
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (client_name, days))
                rows = cur.fetchall()
                cols = [desc[0] for desc in cur.description]
        return pd.DataFrame(rows, columns=cols)
    except Exception as e:
        print(f"[WARN] Não foi possível carregar histórico de '{client_name}': {e}")
        return pd.DataFrame(columns=["read_at", "used"])


# ─────────────────────────────────────────
# API DOMANI
# ─────────────────────────────────────────
def fetch_used_contacts(token: str) -> int:
    try:
        resp = httpx.get(
            f"{DOMANI_BASE}/flow/bot-users-count",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return sum(item["num"] for item in data)
    except Exception as e:
        print(f"[ERRO] {e}")
        return -1


def fetch_total_used(tokens: list[str]) -> int:
    total = 0
    for token in tokens:
        result = fetch_used_contacts(token)
        if result < 0:
            return -1
        total += result
    return total


# ─────────────────────────────────────────
# PREVISÃO COM PANDAS
# ─────────────────────────────────────────
def compute_forecast(df: pd.DataFrame, used: int, limit: int) -> dict:
    forecast = {
        "growth_7d":  None,
        "growth_30d": None,
        "days_left":  None,
        "est_date":   None,
    }

    if df.empty or len(df) < 2:
        return forecast

    df = df.copy()
    df["read_at"] = pd.to_datetime(df["read_at"], utc=True)
    df = df.sort_values("read_at")

    def daily_growth(subset: pd.DataFrame):
        if len(subset) < 2:
            return None
        subset = subset.copy()
        subset["days"] = (
            subset["read_at"] - subset["read_at"].min()
        ).dt.total_seconds() / 86400
        slope = subset["days"].cov(subset["used"]) / subset["days"].var()
        return round(slope, 1)

    cutoff_7d  = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)
    cutoff_30d = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)

    forecast["growth_7d"]  = daily_growth(df[df["read_at"] >= cutoff_7d])
    forecast["growth_30d"] = daily_growth(df[df["read_at"] >= cutoff_30d])

    growth = forecast["growth_7d"] or forecast["growth_30d"]

    if growth and growth > 0:
        saldo = limit - used
        days_left = int(saldo / growth)
        forecast["days_left"] = days_left
        forecast["est_date"]  = (
            datetime.now() + pd.Timedelta(days=days_left)
        ).strftime("%d/%m/%Y")
    elif growth is not None and growth <= 0:
        forecast["days_left"] = 999
        forecast["est_date"]  = "—"

    return forecast


# ─────────────────────────────────────────
# MONTA RELATÓRIO
# ─────────────────────────────────────────
def build_report() -> list[dict]:
    rows = []
    for client in CLIENTS:
        used  = fetch_total_used(client["tokens"])
        limit = client["limit"]

        if used >= 0:
            save_reading(client["name"], used, limit)

        available = limit - used if used >= 0 else None
        pct       = round((used / limit) * 100, 1) if used >= 0 else None

        df       = load_history(client["name"], days=30)
        forecast = compute_forecast(df, used, limit)

        rows.append({
            "name":      client["name"],
            "limit":     limit,
            "used":      used,
            "available": available,
            "pct":       pct,
            "canais":    len(client["tokens"]),
            **forecast,
        })

    return rows


# ─────────────────────────────────────────
# RENDERIZA HTML
# ─────────────────────────────────────────
def status_badge(pct, days_left):
    if pct is None:
        return "<span style='color:#999'>—</span>"
    if pct >= 100 or (days_left is not None and days_left < 14):
        return "<span style='color:#dc2626;font-weight:600'>🔴 Crítico</span>"
    if pct >= 70 or (days_left is not None and days_left < 45):
        return "<span style='color:#f59e0b;font-weight:600'>🟡 Atenção</span>"
    return "<span style='color:#16a34a;font-weight:600'>🟢 OK</span>"


def fmt(n, fallback="—"):
    if n is None or n < 0:
        return fallback
    return f"{n:,}".replace(",", ".")


def render_html(rows: list[dict], password: str) -> str:
    now = datetime.now().strftime("%d/%m/%Y às %H:%M")

    table_rows = ""
    for r in rows:
        pct_val   = r["pct"] or 0
        bar_pct   = min(pct_val, 100)
        bar_color = (
            "#dc2626" if bar_pct >= 90
            else "#f59e0b" if bar_pct >= 70
            else "#16a34a"
        )
        pct_str = f"{r['pct']}%" if r["pct"] is not None else "—"
        badge   = status_badge(r["pct"], r["days_left"])

        progress = f"""
        <div style='background:#e5e7eb;border-radius:4px;height:8px;width:100px;
                    display:inline-block;vertical-align:middle'>
          <div style='background:{bar_color};width:{bar_pct}%;height:8px;border-radius:4px'></div>
        </div>
        <span style='margin-left:6px;font-size:12px;color:#6b7280'>{pct_str}</span>
        """

        g7  = f"+{r['growth_7d']}/dia"  if r["growth_7d"]  and r["growth_7d"]  > 0 else "—"
        g30 = f"+{r['growth_30d']}/dia" if r["growth_30d"] and r["growth_30d"] > 0 else "—"

        if r["days_left"] is None:
            prev = "<span style='color:#9ca3af'>Sem dados</span>"
        elif r["days_left"] == 999:
            prev = "<span style='color:#16a34a'>Sem crescimento</span>"
        else:
            prev = f"{r['days_left']} dias &nbsp;<span style='color:#6b7280'>({r['est_date']})</span>"

        canais_badge = (
            f"<span style='font-size:11px;color:#9ca3af'>{r['canais']} canais</span>"
            if r["canais"] > 1 else ""
        )

        table_rows += f"""
        <tr style='border-bottom:1px solid #f3f4f6'>
          <td style='padding:14px 16px;font-weight:600;color:#111827'>
            {r['name']}<br>{canais_badge}
          </td>
          <td style='padding:14px 16px;text-align:right;color:#374151'>{fmt(r['limit'])}</td>
          <td style='padding:14px 16px;text-align:right;color:#374151'>{fmt(r['used'])}</td>
          <td style='padding:14px 16px;text-align:right;color:#374151'>{fmt(r['available'])}</td>
          <td style='padding:14px 16px'>{progress}</td>
          <td style='padding:14px 16px;color:#374151;font-size:13px'>
            <div>7d: {g7}</div>
            <div style='color:#9ca3af'>30d: {g30}</div>
          </td>
          <td style='padding:14px 16px;font-size:13px'>{prev}</td>
          <td style='padding:14px 16px'>{badge}</td>
        </tr>
        """

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Monitor de Contatos — Domani</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: Inter, Arial, sans-serif; background: #f9fafb; }}
    #lock-screen {{
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh; background: #f9fafb;
    }}
    #lock-box {{
      background: #fff; border-radius: 12px; padding: 40px;
      box-shadow: 0 1px 4px rgba(0,0,0,.1); text-align: center; width: 320px;
    }}
    #lock-box h2 {{ font-size: 18px; color: #111827; margin-bottom: 8px; }}
    #lock-box p  {{ font-size: 13px; color: #6b7280; margin-bottom: 24px; }}
    #lock-box input {{
      width: 100%; padding: 10px 14px; border: 1px solid #d1d5db;
      border-radius: 8px; font-size: 14px; margin-bottom: 12px; outline: none;
    }}
    #lock-box input:focus {{ border-color: #111827; }}
    #lock-box button {{
      width: 100%; padding: 10px; background: #111827; color: #fff;
      border: none; border-radius: 8px; font-size: 14px; cursor: pointer;
    }}
    #lock-box button:hover {{ background: #1f2937; }}
    #error-msg {{ color: #dc2626; font-size: 13px; margin-top: 8px; display: none; }}
    #report {{ display: none; padding: 32px; }}
    .card {{
      max-width: 960px; margin: 0 auto; background: #fff;
      border-radius: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.08); overflow: hidden;
    }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ padding: 12px 16px; }}
    @media (max-width: 600px) {{
      th, td {{ padding: 8px 10px; font-size: 12px; }}
      #report {{ padding: 16px; }}
    }}
  </style>
</head>
<body>

<div id="lock-screen">
  <div id="lock-box">
    <h2>📊 Monitor Domani</h2>
    <p>Digite a senha para acessar o relatório</p>
    <input type="password" id="pwd-input" placeholder="Senha"
           onkeydown="if(event.key==='Enter') checkPassword()" autofocus>
    <button onclick="checkPassword()">Entrar</button>
    <div id="error-msg">Senha incorreta</div>
  </div>
</div>

<div id="report">
  <div class="card">
    <div style='background:#111827;padding:24px 32px'>
      <h2 style='color:#fff;margin:0;font-size:18px'>📊 Monitor de Contatos — Domani</h2>
      <p style='color:#9ca3af;margin:4px 0 0;font-size:13px'>Gerado em {now}</p>
    </div>
    <div style='overflow-x:auto'>
      <table>
        <thead>
          <tr style='background:#f3f4f6;border-bottom:2px solid #e5e7eb'>
            <th style='text-align:left;color:#6b7280;font-weight:600'>Cliente</th>
            <th style='text-align:right;color:#6b7280;font-weight:600'>Limite</th>
            <th style='text-align:right;color:#6b7280;font-weight:600'>Utilizado</th>
            <th style='text-align:right;color:#6b7280;font-weight:600'>Disponível</th>
            <th style='color:#6b7280;font-weight:600'>Uso</th>
            <th style='color:#6b7280;font-weight:600'>Crescimento</th>
            <th style='color:#6b7280;font-weight:600'>Esgotamento</th>
            <th style='color:#6b7280;font-weight:600'>Status</th>
          </tr>
        </thead>
        <tbody>
          {table_rows}
        </tbody>
      </table>
    </div>
    <div style='padding:16px 32px;border-top:1px solid #f3f4f6;font-size:12px;color:#9ca3af'>
      🟢 OK &nbsp;|&nbsp;
      🟡 Atenção: uso &gt;70% ou esgotamento em menos de 45 dias &nbsp;|&nbsp;
      🔴 Crítico: uso &gt;90% ou esgotamento em menos de 14 dias<br>
      Crescimento por regressão linear. Previsão baseada nos últimos 7 dias.
    </div>
  </div>
</div>

<script>
  const CORRECT = "{password}";
  const SESSION_KEY = "domani_auth";

  function checkPassword() {{
    const val = document.getElementById("pwd-input").value;
    if (val === CORRECT) {{
      sessionStorage.setItem(SESSION_KEY, "1");
      showReport();
    }} else {{
      document.getElementById("error-msg").style.display = "block";
    }}
  }}

  function showReport() {{
    document.getElementById("lock-screen").style.display = "none";
    document.getElementById("report").style.display = "block";
  }}

  if (sessionStorage.getItem(SESSION_KEY) === "1") showReport();
</script>
</body>
</html>"""


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Gera o relatório localmente sem salvar no Supabase"
    )
    args = parser.parse_args()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando monitoramento...")
    rows = build_report()

    for r in rows:
        days = f"{r['days_left']}d" if r["days_left"] else "—"
        canais = f" ({r['canais']} canais)" if r["canais"] > 1 else ""
        print(f"  {r['name']}{canais}: {r['used']}/{r['limit']} ({r['pct']}%) — esgota em {days}")

    html = render_html(rows, REPORT_PASSWORD)

    output = "preview.html" if args.dry_run else "report.html"
    with open(output, "w") as f:
        f.write(html)

    if args.dry_run:
        import webbrowser
        webbrowser.open(pathlib.Path(output).resolve().as_uri())
        print("[DRY-RUN] Abrindo preview no browser (Supabase não foi alterado)...")
    else:
        print(f"[OK] Relatório salvo em {output}")