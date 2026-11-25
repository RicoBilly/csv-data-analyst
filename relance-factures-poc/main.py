"""Agent de relance des factures clients Pennylane (v9, phase 2 en dry-run par defaut).

L'agent lit les factures clients (dates d'emission, echeances, paiements, clients) :
- via l'API Company v2 (outils fetch_*, token lu depuis la variable d'environnement
  PENNYLANE_API_TOKEN injectee par le deploiement a partir du secret workspace ; le
  token n'est jamais un argument ni affiche), le MCP renvoyant des 500 sur les
  endpoints factures pour cette entreprise ;
- via le connecteur MCP Pennylane en secours (comptes 411, clients, paiements).
Il applique les regles de relance (J = date d'echeance ; seuils J+15 / J+30 / J+45 /
J+60 / J+90 ; exclusion de la 1re relance quand date d'emission = date d'echeance ;
exclusion des factures en paiement partiel, encaissees par prelevement automatique) de
facon deterministe via l'outil compute_relance_report.

Phase 2 (v9) : plan du jour filtre par le journal Drive anti-doublon (fetch_relance_plan),
envois e-mail Pennylane J+15/30/45 via POST send_by_email (send_pennylane_invoice_email,
dry-run par defaut via RELANCE_DRY_RUN=true), brouillon Gmail avec PDF en PJ pour J+60
(fetch_invoice_pdf_base64 + gmail_create_draft), lettre de mise en demeure stockee dans
Drive pour J+90. Le garde-fou RelanceGuardrail n'autorise que : outils client.*, lectures
Pennylane, gmail_create_draft (+ lectures Gmail), lectures/creations/mises a jour Drive.
Aucun envoi Gmail automatique, aucune suppression, run_operations bloque.
"""

import base64
import json
import os
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Any

from mistralai import agents

PENNYLANE_PREFIX = "connector_pennylane_e376."
PENNYLANE_API_BASE = "https://app.pennylane.com/api/external/v2"
# Nom principal attendu ; repli sur le nom du secret si le binding console a repris
# le nom du secret comme nom de variable d'env.
API_TOKEN_ENV = "PENNYLANE_API_TOKEN"
API_TOKEN_ENV_FALLBACK = "cle_api_pennylane"

# Config phase 2 (envois reels) - valeurs fournies et VERIFIEES le 2026-11-23 :
# - compte Gmail du connecteur gmail : eric.doyen@id-interactive.fr (verifie via from:me)
# - dossier Drive "Relance-factures" : id 1D3gWJnITWIpHHUKo13jgBClAmzWInDqP, owner
#   eric.doyen@id-interactive.fr, can_add_children=true, vide a la creation
# - modeles d'email Pennylane de relance impayee (kind billing_email_invoice), a
#   resoudre en id via fetch_pennylane_email_templates avant tout send_by_email
PHASE2_CONFIG = {
    "gmail_account": "eric.doyen@id-interactive.fr",
    "drive_folder_title": "Relance-factures",
    "drive_folder_id": "1D3gWJnITWIpHHUKo13jgBClAmzWInDqP",
    "relance_email_templates": {
        1: "Relance impayée j+15",
        2: "Relance impayée j+30",
        3: "Relance impayée j+45",
    },
}

# Phase 2 (v9) : securites par defaut
RELANCE_DRY_RUN_ENV = "RELANCE_DRY_RUN"          # "true" par defaut : aucun envoi reel tant que non desactive explicitement
RELANCE_DAILY_CAP_ENV = "RELANCE_DAILY_SEND_CAP"  # plafond d'envois e-mail Pennylane reels par jour
DEFAULT_DAILY_SEND_CAP = 20
PDF_MAX_BYTES = 3_000_000                         # limite PJ brouillon Gmail (~3 Mo)
JOURNAL_FILENAME = "journal-relances.json"
REPORTS_FOLDER = "rapports"
LETTERS_FOLDER = "Mises en demeure"
GMAIL_PREFIX = "connector_gmail."
DRIVE_PREFIX = "connector_google_drive_mcp."

# Outils curates officiels du MCP Pennylane (allow-list 100% lecture seule cote serveur) :
# https://pennylane-mcp.readme.io/docs/getting-started
CURATED_READ_TOOLS = {
    # contexte & societes
    "get_context",
    "get_company",
    "list_companies",
    "list_firm_file",
    # clients & creances
    "list_customers",
    "get_customer",
    "list_customer_invoices",
    "get_customer_invoice",
    "list_customer_payments",
    # fournisseurs & dettes
    "list_suppliers",
    "get_supplier",
    "list_supplier_invoices",
    "get_supplier_invoice",
    "list_supplier_payments",
    "get_supplier_payment",
    # comptabilite & transactions bancaires
    "list_accounts",
    "get_ledger_account",
    "get_journal",
    "list_ledger_entry_lines",
    "get_entry",
    "list_transactions",
    "get_transaction",
    "query_trial_balance",
    # produits
    "list_products",
    "get_product",
    # reporting & fiscalite
    "get_financial_report",
    "get_vat_returns",
    "get_tax_returns",
    # audit
    "get_changelog",
    # meta-outils du catalogue prive (run_operations reste soumis au blocage anti-ecriture)
    "search_operations",
    "run_operations",
}

# Tout outil curate Pennylane nomme list_*, get_* ou query_* est en lecture seule par
# construction (allow-list serveur) : on l'autorise meme s'il n'est pas dans la liste
# ci-dessus, pour rester robuste si Pennylane ajoute des outils curates.
READ_PREFIXES = ("list_", "get_", "query_")

STAGES = [
    {"threshold": 15, "level": 1, "label": "1re relance - simple rappel", "tone": "tres cordial", "channel": "email automatique Pennylane"},
    {"threshold": 30, "level": 2, "label": "2e relance - facture toujours en attente", "tone": "courtois", "channel": "email automatique Pennylane"},
    {"threshold": 45, "level": 3, "label": "3e relance - demande de regularisation", "tone": "plus ferme", "channel": "email automatique Pennylane"},
    {"threshold": 60, "level": 4, "label": "4e relance - dernier rappel amiable", "tone": "ferme mais respectueux", "channel": "email + appel cible (brouillon Gmail avec facture en PJ)"},
    {"threshold": 90, "level": 5, "label": "Mise en demeure", "tone": "formel", "channel": "courrier recommande (redaction + stockage Drive, envoi manuel)"},
]


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _pick(inv: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in inv and inv[k] not in (None, ""):
            return inv[k]
    return None


def _to_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


@agents.tool
def compute_relance_report(invoices_json: str, today: str | None = None) -> dict[str, Any]:
    """Calcule de facon deterministe le plan de relance a partir des factures clients.

    invoices_json : liste JSON d'objets avec les champs : invoice_id (ou id), number,
        customer_id, customer_name, customer_email, date (date d'emission), deadline
        (date d'echeance / date de paiement demande), currency, amount (montant TTC),
        amount_paid (deja paye), status.
    today : date du jour au format YYYY-MM-DD (laisse vide pour utiliser la date courante).

    Regles : J = date d'echeance. Seuils J+15, J+30, J+45, J+60, J+90. Une facture dont la
    date d'emission est identique a sa date d'echeance n'est PAS sujette a la 1re relance.
    Les factures soldees ou non encore echues sont exclues. Une facture ayant recu un
    PAIEMENT PARTIEL (amount_paid > 0 et reste du > 0) n'est JAMAIS relancee : le solde
    est encaisse par prelevement automatique (bucket excluded_partial_payment).

    Retourne un dict avec le resume, la liste des factures par etape et un rapport
    markdown (champ report_markdown).
    """
    invoices = json.loads(invoices_json)
    if not isinstance(invoices, list):
        raise agents.ToolError("invoices_json doit etre une liste JSON d'objets facture.")
    if today:
        run_date = _parse_date(today)
        if run_date is None:
            raise agents.ToolError("today doit etre au format YYYY-MM-DD.")
    else:
        run_date = agents.now().date()

    items: list[dict[str, Any]] = []
    paid: list[dict[str, Any]] = []
    not_due: list[dict[str, Any]] = []
    excluded_first: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []

    for inv in invoices:
        issue = _parse_date(_pick(inv, "date", "issue_date"))
        deadline = _parse_date(_pick(inv, "deadline", "due_date"))
        if deadline is None:
            continue
        amount = _to_float(_pick(inv, "amount", "currency_amount", "amount_ttc"))
        amount_paid = _to_float(_pick(inv, "amount_paid", "paid_amount", "paid"))
        amount_due = round(amount - amount_paid, 2)
        entry = {
            "invoice_id": _pick(inv, "invoice_id", "id"),
            "number": _pick(inv, "number", "invoice_number"),
            "customer_id": _pick(inv, "customer_id"),
            "customer_name": _pick(inv, "customer_name", "customer"),
            "customer_email": _pick(inv, "customer_email", "email"),
            "date": issue.isoformat() if issue else None,
            "deadline": deadline.isoformat(),
            "currency": _pick(inv, "currency") or "EUR",
            "amount_due": amount_due,
            "amount_paid": amount_paid,
            "status": _pick(inv, "status"),
        }
        if amount_due <= 0.005:
            paid.append(entry)
            continue
        if amount_paid > 0.005:
            entry["note"] = "paiement partiel recu -> prelevement automatique, hors relances"
            partial.append(entry)
            continue
        days_late = (run_date - deadline).days
        entry["days_late"] = days_late
        if days_late < 0:
            not_due.append(entry)
            continue
        stage = None
        for s in STAGES:
            if days_late >= s["threshold"]:
                stage = s
        if stage is None:
            not_due.append(entry)
            continue
        if stage["level"] == 1 and issue is not None and issue == deadline:
            entry["stage"] = None
            entry["note"] = "exclusion 1re relance : date d'emission = date d'echeance"
            excluded_first.append(entry)
            continue
        entry["stage"] = stage
        items.append(entry)

    items.sort(key=lambda e: (-(e["stage"]["level"]), -e["days_late"]))
    by_stage: dict[str, list[dict[str, Any]]] = {}
    for e in items:
        by_stage.setdefault(str(e["stage"]["level"]), []).append(e)

    total_due = round(sum(e["amount_due"] for e in items), 2)
    partial_due = round(sum(e["amount_due"] for e in partial), 2)
    currency = items[0]["currency"] if items else "EUR"

    lines = [
        f"# Rapport a blanc des relances - {run_date.isoformat()}",
        "",
        "POC lecture seule : aucun envoi effectue, aucune donnee modifiee.",
        "",
        f"- Factures en retard avec action prevue : **{len(items)}** (encours **{total_due:.2f} {currency}**)",
        f"- Factures soldees ignorees : {len(paid)}",
        f"- Factures non echues / hors fenetre de relance : {len(not_due)}",
        f"- Factures exclues de la 1re relance (emission = echeance) : {len(excluded_first)}",
        f"- Factures en paiement partiel, HORS relances (prelevement automatique) : {len(partial)} (reste du {partial_due:.2f} EUR)",
        "",
    ]
    if items:
        lines += [
            "| Facture | Client | Email | Montant du | Echeance (J) | Jours de retard | Etape | Action prevue | Ton |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for e in items:
            s = e["stage"]
            lines.append(
                f"| {e['number'] or e['invoice_id']} | {e['customer_name']} | {e['customer_email']} | "
                f"{e['amount_due']:.2f} {e['currency']} | {e['deadline']} | {e['days_late']} | "
                f"{s['level']} ({s['label']}) | {s['channel']} | {s['tone']} |"
            )
        lines.append("")
    if excluded_first:
        lines += ["## Exclusions (date d'emission = date d'echeance, 1re relance non applicable)", ""]
        for e in excluded_first:
            lines.append(f"- {e['number'] or e['invoice_id']} ({e['customer_name']}) - echeance {e['deadline']} - {e['days_late']} j de retard")
        lines.append("")
    if partial:
        lines += ["## Hors relances : paiement partiel recu (prelevement automatique)", ""]
        for e in partial:
            lines.append(
                f"- {e['number'] or e['invoice_id']} ({e['customer_name']}) - echeance {e['deadline']} - "
                f"reste du {e['amount_due']:.2f} {e['currency']} - deja paye {e['amount_paid']:.2f} {e['currency']}"
            )
        lines.append("")

    report_markdown = "\n".join(lines)
    return {
        "today": run_date.isoformat(),
        "overdue_count": len(items),
        "total_due": total_due,
        "currency": currency,
        "by_stage": {k: len(v) for k, v in by_stage.items()},
        "paid_ignored": len(paid),
        "not_due": len(not_due),
        "excluded_first_relance": [e["invoice_id"] for e in excluded_first],
        "excluded_partial_payment": [e["invoice_id"] for e in partial],
        "partial_payment_count": len(partial),
        "partial_payment_due": partial_due,
        "items": items,
        "report_markdown": report_markdown,
    }


@agents.tool
def compute_encours_report(accounts_json: str, customers_json: str, payments_json: str, today: str | None = None) -> dict[str, Any]:
    """Rapport a blanc NIVEAU ENCOURS clients, calcule de facon deterministe.

    Utilise quand l'endpoint factures est indisponible (erreurs 500 cote serveur MCP) :
    l'encours de chaque client = solde debiteur (total_balance) de son compte 411.

    accounts_json : liste JSON de comptes 411 {number, label, total_balance, ...}
        retournes par list_accounts(number_prefix="411").
    customers_json : liste JSON de clients {id, name, emails, ...} retournes par
        list_customers. Le rapprochement compte <-> client se fait sur le nom (label).
    payments_json : liste JSON de paiements {invoice_id, customer_id, customer_name,
        amount, date, status, method} retournes par list_customer_payments.
    today : date du jour YYYY-MM-DD (vide = date courante).

    Retourne un dict avec le resume (total encours, nb clients debiteurs, paiements
    recents) et un rapport markdown (champ report_markdown) listant les clients debiteurs
    tries par encours decroissant avec leur email. AUCUNE action de relance n'est
    effectuee : le plan facture par facture (J+15/J+30/J+45/J+60/J+90) necessite les
    dates d'echeance des factures, indisponibles via le MCP pour cette entreprise.
    """
    accounts = json.loads(accounts_json)
    customers = json.loads(customers_json)
    payments = json.loads(payments_json)
    if not isinstance(accounts, list) or not isinstance(customers, list) or not isinstance(payments, list):
        raise agents.ToolError("accounts_json, customers_json et payments_json doivent etre des listes JSON.")
    if today:
        run_date = _parse_date(today)
        if run_date is None:
            raise agents.ToolError("today doit etre au format YYYY-MM-DD.")
    else:
        run_date = agents.now().date()

    def norm(s: Any) -> str:
        return " ".join(str(s or "").upper().split())

    cust_by_name = {norm(c.get("name")): c for c in customers if c.get("name")}

    debtors: list[dict[str, Any]] = []
    for a in accounts:
        balance = _to_float(a.get("total_balance"))
        if balance <= 0.005:
            continue
        cust = cust_by_name.get(norm(a.get("label")), {})
        emails = cust.get("emails") or []
        debtors.append({
            "account_number": a.get("number"),
            "account_label": a.get("label"),
            "customer_id": cust.get("id"),
            "customer_name": cust.get("name") or a.get("label"),
            "customer_email": emails[0] if emails else None,
            "emails": emails,
            "balance": round(balance, 2),
        })
    debtors.sort(key=lambda d: -d["balance"])
    total_due = round(sum(d["balance"] for d in debtors), 2)

    pay_total = round(sum(_to_float(p.get("amount")) for p in payments), 2)
    pay_recent = [p for p in payments if (p.get("date") or "")[:10] >= "2026-10-01"]

    lines = [
        f"# Rapport a blanc des encours clients - {run_date.isoformat()}",
        "",
        "POC lecture seule : aucun envoi effectue, aucune donnee modifiee.",
        "",
        f"- Clients debiteurs (compte 411, solde > 0) : **{len(debtors)}**",
        f"- Encours total : **{total_due:.2f} EUR**",
        f"- Paiements clients lus : {len(payments)} (total {pay_total:.2f} EUR), dont {len(pay_recent)} depuis le 2026-10-01",
        "",
        "ATTENTION : le plan de relance facture par facture (J = echeance ; seuils J+15/J+30/J+45/J+60/J+90 ;",
        "exclusion de la 1re relance si emission = echeance) N'est PAS calcule ici : les outils MCP",
        "list_customer_invoices / get_customer_invoice / search_operations renvoient une erreur 500 pour",
        "cette entreprise, donc les dates d'echeance facture par facture sont indisponibles via le MCP.",
        "A calculer via l'API Company v2 (token) ou une fois le bug MCP corrige.",
        "",
    ]
    if debtors:
        lines += [
            "| Compte 411 | Client | Email | Encours (EUR) |",
            "|---|---|---|---|",
        ]
        for d in debtors:
            lines.append(f"| {d['account_number']} | {d['customer_name']} | {d['customer_email'] or ''} | {d['balance']:.2f} |")
        lines.append("")
    if pay_recent:
        lines += ["## Paiements clients recents (depuis 2026-10-01)", ""]
        for p in sorted(pay_recent, key=lambda p: str(p.get("date")), reverse=True)[:30]:
            lines.append(f"- {str(p.get('date'))[:10]} - {p.get('customer_name')} - facture {p.get('invoice_id')} - {p.get('amount')} {p.get('currency', 'EUR')} - {p.get('status')}")
        lines.append("")

    return {
        "today": run_date.isoformat(),
        "debtor_count": len(debtors),
        "total_due": total_due,
        "payments_count": len(payments),
        "payments_total": pay_total,
        "debtors": debtors,
        "report_markdown": "\n".join(lines),
    }


def _api_v2_token() -> str:
    """Token API lu UNIQUEMENT depuis l'environnement du worker (jamais un argument, jamais affiche)."""
    token = next(
        (os.environ[v].strip() for v in (API_TOKEN_ENV, API_TOKEN_ENV_FALLBACK) if os.environ.get(v, "").strip()),
        "",
    )
    if not token:
        raise agents.ToolError(
            f"Variable d'environnement {API_TOKEN_ENV} (ou {API_TOKEN_ENV_FALLBACK}) absente : "
            "le secret workspace (cle_api_pennylane) n'est pas injecte dans ce deploiement."
        )
    return token


def _api_v2_request(method: str, path: str, params: dict[str, Any], company_id: int, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    """Requete brute API v2. Retourne (status HTTP, payload JSON ou texte tronque).

    Ne leve ToolError que sur erreur reseau (URLError) ; les HTTP 4xx/5xx sont
    retournes au caller (status, payload) pour qu'il decide (ex. 409 = retry).
    """
    token = _api_v2_token()
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{PENNYLANE_API_BASE}{path}" + (f"?{query}" if query else "")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "X-Company-Id": str(company_id),
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except urllib.error.URLError as e:
        raise agents.ToolError(f"API Pennylane v2 {path} injoignable: {e.reason}")
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = raw[:2000]
    return status, payload


def _api_v2_get(path: str, params: dict[str, Any], company_id: int) -> dict[str, Any]:
    """GET sur l'API Company v2 Pennylane. Le token vient UNIQUEMENT de l'env.

    Leve ToolError sur HTTP >= 400 (message avec status + extrait de body, jamais de token).
    """
    status, payload = _api_v2_request("GET", path, params, company_id)
    if status >= 400:
        body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)[:300]
        raise agents.ToolError(f"API Pennylane v2 {path} -> HTTP {status}: {body}")
    if not isinstance(payload, dict):
        raise agents.ToolError(f"API Pennylane v2 {path} -> reponse inattendue (HTTP {status})")
    return payload


def _api_v2_post(path: str, params: dict[str, Any], company_id: int, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    """POST sur l'API Company v2 Pennylane. Retourne (status, payload) SANS lever sur 4xx/5xx."""
    return _api_v2_request("POST", path, params, company_id, body)


def _download_bytes(url: str, max_bytes: int) -> bytes:
    """Telecharge une URL (ex. public_file_url signe) en lisant au plus max_bytes + 1 octets."""
    req = urllib.request.Request(url, headers={"Accept": "application/pdf"}, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read(max_bytes + 1)


SKIP_STATUSES = {"draft", "credit_note", "cancelled", "partially_cancelled", "archived"}


def _map_v2_invoice(item: dict[str, Any], cust_by_id: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    """Mappe une facture API v2 vers les alias attendus par compute_relance_report."""
    status = item.get("status") or ""
    if status in SKIP_STATUSES or item.get("draft") or item.get("credit_note"):
        return None
    amount = _to_float(item.get("currency_amount") or item.get("amount"))
    remaining = item.get("remaining_amount_with_tax")
    if item.get("paid"):
        amount_paid = amount
    elif remaining is not None:
        amount_paid = round(amount - _to_float(remaining), 2)
    else:
        amount_paid = 0.0
    customer = item.get("customer") or {}
    cust = cust_by_id.get(customer.get("id"), {})
    emails = cust.get("emails") or ([cust["email"]] if cust.get("email") else [])
    return {
        "invoice_id": item.get("id"),
        "number": item.get("invoice_number") or item.get("label"),
        "customer_id": customer.get("id"),
        "customer_name": cust.get("name") or f"client {customer.get('id')}",
        "customer_email": emails[0] if emails else None,
        "date": item.get("date"),
        "deadline": item.get("deadline"),
        "currency": item.get("currency") or "EUR",
        "amount": item.get("currency_amount") or item.get("amount"),
        "amount_paid": amount_paid,
        "status": status,
    }


def _fetch_v2_customers(company_id: int, max_pages: int = 20) -> dict[int, dict[str, Any]]:
    """GET /customers pagine (cursor) -> {id: client}."""
    cust_by_id: dict[int, dict[str, Any]] = {}
    cursor = None
    for _ in range(max_pages):
        data = _api_v2_get("/customers", {"limit": 100, "cursor": cursor}, company_id)
        for c in data.get("items", []):
            if c.get("id") is not None:
                cust_by_id[c["id"]] = c
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(0.3)
    return cust_by_id


def _fetch_v2_invoices(company_id: int, cust_by_id: dict[int, dict[str, Any]], max_pages: int = 40) -> tuple[list[dict[str, Any]], int, int]:
    """GET /customer_invoices pagine (draft=false, credit_note=false) -> (factures mappees, pages, nb vus)."""
    invoices: list[dict[str, Any]] = []
    seen = 0
    pages = 0
    cursor = None
    flt = json.dumps([
        {"field": "draft", "operator": "eq", "value": "false"},
        {"field": "credit_note", "operator": "eq", "value": "false"},
    ])
    while pages < max_pages:
        data = _api_v2_get("/customer_invoices", {"limit": 100, "cursor": cursor, "filter": flt, "sort": "-id"}, company_id)
        pages += 1
        for item in data.get("items", []):
            seen += 1
            mapped = _map_v2_invoice(item, cust_by_id)
            if mapped is not None:
                invoices.append(mapped)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(0.3)
    return invoices, pages, seen


def _dry_run_default() -> bool:
    return os.environ.get(RELANCE_DRY_RUN_ENV, "true").strip().lower() not in ("0", "false", "no", "")


def _daily_send_cap() -> int:
    try:
        return max(1, int(os.environ.get(RELANCE_DAILY_CAP_ENV, str(DEFAULT_DAILY_SEND_CAP)).strip()))
    except ValueError:
        return DEFAULT_DAILY_SEND_CAP


def _parse_journal(journal_json: str) -> list[dict[str, Any]]:
    try:
        journal = json.loads(journal_json or "[]")
    except json.JSONDecodeError as e:
        raise agents.ToolError(f"journal_json invalide (liste JSON attendue) : {e}")
    if not isinstance(journal, list):
        raise agents.ToolError("journal_json doit etre une liste JSON d'entrees {invoice_id, stage, channel, at, dry_run}.")
    return journal


def _journal_has(journal: list[dict[str, Any]], invoice_id: Any, stage: int) -> bool:
    """True si (invoice_id, stage) a deja ete relance REELLEMENT (les entrees dry_run ne bloquent pas)."""
    return any(
        e.get("invoice_id") == invoice_id and e.get("stage") == stage and not e.get("dry_run")
        for e in journal
    )


def _journal_today_email_sends(journal: list[dict[str, Any]], today: str) -> int:
    return sum(
        1
        for e in journal
        if e.get("channel") == "pennylane_email" and not e.get("dry_run") and str(e.get("at", ""))[:10] == today
    )


def _stage_for(deadline: date | None, issue: date | None, run_date: date) -> tuple[dict[str, Any] | None, int]:
    """Palier applicable a une facture a run_date (memes regles que compute_relance_report)."""
    if deadline is None:
        return None, -1
    days_late = (run_date - deadline).days
    if days_late < 0:
        return None, days_late
    stage = None
    for s in STAGES:
        if days_late >= s["threshold"]:
            stage = s
    if stage is None:
        return None, days_late
    if stage["level"] == 1 and issue is not None and issue == deadline:
        return None, days_late
    return stage, days_late


def _resolve_template_id(company_id: int, stage: int) -> dict[str, Any] | None:
    """Resout le modele d'email Pennylane du palier (nom depuis PHASE2_CONFIG) via GET /billing_email_templates."""
    wanted = _norm_key(PHASE2_CONFIG["relance_email_templates"][stage])
    data = _api_v2_get("/billing_email_templates", {"limit": 100}, company_id)
    for it in data.get("items", []):
        blob = _norm_key(" ".join(str(v) for v in it.values() if isinstance(v, str)))
        if wanted in blob:
            return it
    return None


@agents.tool
def fetch_pennylane_customer_invoices(company_id: int, max_pages: int = 40) -> dict[str, Any]:
    """Lit les factures clients Pennylane via l'API Company v2 (lecture seule, GET uniquement).

    company_id : identifiant Pennylane de l'entreprise (fourni par l'utilisateur, ex.
        21994007), transmis en header X-Company-Id.
    max_pages : nombre max de pages de 100 factures (securite, defaut 40 = 4000 factures).

    Le token API est lu automatiquement depuis l'environnement du worker
    (PENNYLANE_API_TOKEN, ou cle_api_pennylane selon le binding ; secret workspace injecte
    au deploiement) : ne JAMAIS le passer en argument et ne jamais l'afficher.
    Appels effectues (GET, aucune ecriture) :
    GET /api/external/v2/customer_invoices?limit=100&filter=[draft=false, credit_note=false]
    pagine par cursor, puis GET /api/external/v2/customers pagine pour les noms/emails.

    Retourne un dict : invoices_count, customers_count, source="pennylane_api_v2" et
    invoices_json (liste JSON prete pour compute_relance_report : invoice_id, number,
    customer_id, customer_name, customer_email, date, deadline, currency, amount,
    amount_paid, status).
    """
    if not isinstance(company_id, int) or company_id <= 0:
        raise agents.ToolError("company_id doit etre un entier positif (ex. 21994007).")

    try:
        cust_by_id = _fetch_v2_customers(company_id)
    except agents.ToolError:
        cust_by_id = {}
    invoices, pages, _seen = _fetch_v2_invoices(company_id, cust_by_id, max_pages)

    return {
        "company_id": company_id,
        "source": "pennylane_api_v2",
        "pages_fetched": pages,
        "invoices_count": len(invoices),
        "customers_count": len(cust_by_id),
        "invoices_json": json.dumps(invoices, ensure_ascii=False),
    }


@agents.tool
def top_debtors_report(invoices_json: str, accounts_json: str = "[]", today: str | None = None, top_n: int = 20) -> dict[str, Any]:
    """Rapport TOP N des clients debiteurs, en croisant la vue facture (API v2) et la vue compte 411 (MCP).

    invoices_json : liste JSON de factures (meme format que compute_relance_report :
        invoice_id, number, customer_id, customer_name, customer_email, date, deadline,
        currency, amount, amount_paid, status).
    accounts_json : liste JSON des comptes 411 retournes par list_accounts
        (number_prefix="411") : {number, label, total_balance, ...}. Le rapprochement
        compte <-> client se fait sur le nom normalise (label du compte 411 = nom client).
    today : date du jour YYYY-MM-DD (vide = date courante). top_n : taille du top (defaut 20).

    Pour chaque client : nombre de factures en retard (echeance <= today et reste du > 0)
    et total du reste du, croises avec le solde du/des compte(s) 411 correspondant(s).
    Les factures en paiement partiel (amount_paid > 0) sont signalees a part
    (partial_invoices / partial_due) : elles restent dans l'encours mais sont HORS
    relances (prelevement automatique).
    Retourne un dict avec debtor_count, total_due, top (liste triee) et report_markdown
    (tableau : rang, client, email, factures en retard, reste du, solde 411, ecart).
    """
    invoices = json.loads(invoices_json)
    accounts = json.loads(accounts_json)
    if not isinstance(invoices, list) or not isinstance(accounts, list):
        raise agents.ToolError("invoices_json et accounts_json doivent etre des listes JSON.")
    if today:
        run_date = _parse_date(today)
        if run_date is None:
            raise agents.ToolError("today doit etre au format YYYY-MM-DD.")
    else:
        run_date = agents.now().date()
    if not isinstance(top_n, int) or top_n <= 0:
        raise agents.ToolError("top_n doit etre un entier positif.")

    def norm(s: Any) -> str:
        return " ".join(str(s or "").upper().split())

    acc_by_name: dict[str, dict[str, Any]] = {}
    for a in accounts:
        key = norm(a.get("label"))
        if not key:
            continue
        cur = acc_by_name.setdefault(key, {"balance": 0.0, "numbers": []})
        cur["balance"] += _to_float(a.get("total_balance"))
        cur["numbers"].append(a.get("number"))

    debtors: dict[str, dict[str, Any]] = {}
    for inv in invoices:
        deadline = _parse_date(_pick(inv, "deadline", "due_date"))
        if deadline is None or deadline > run_date:
            continue
        amount = _to_float(_pick(inv, "amount", "currency_amount", "amount_ttc"))
        amount_paid = _to_float(_pick(inv, "amount_paid", "paid_amount", "paid"))
        amount_due = round(amount - amount_paid, 2)
        if amount_due <= 0.005:
            continue
        name = str(_pick(inv, "customer_name", "customer") or "client inconnu")
        key = norm(name)
        cur = _pick(inv, "currency") or "EUR"
        d = debtors.setdefault(key, {
            "customer_name": name,
            "customer_email": _pick(inv, "customer_email", "email"),
            "invoices_overdue": 0,
            "total_due": 0.0,
            "partial_invoices": 0,
            "partial_due": 0.0,
            "currency": cur,
            "non_eur": False,
        })
        if cur != d["currency"]:
            d["non_eur"] = True
        d["invoices_overdue"] += 1
        d["total_due"] = round(d["total_due"] + amount_due, 2)
        if amount_paid > 0.005:
            d["partial_invoices"] += 1
            d["partial_due"] = round(d["partial_due"] + amount_due, 2)

    rows = []
    for key, d in debtors.items():
        acc = acc_by_name.get(key)
        rows.append({
            "customer_name": d["customer_name"],
            "customer_email": d["customer_email"],
            "invoices_overdue": d["invoices_overdue"],
            "total_due": d["total_due"],
            "partial_invoices": d["partial_invoices"],
            "partial_due": d["partial_due"],
            "currency": "EUR" if not d["non_eur"] else d["currency"] + " (mixte)",
            "balance_411": round(acc["balance"], 2) if acc else None,
            "accounts_411": acc["numbers"] if acc else [],
            "gap_411_minus_invoices": round(acc["balance"] - d["total_due"], 2) if acc else None,
        })
    rows.sort(key=lambda r: -r["total_due"])
    top = rows[:top_n]
    total_due = round(sum(r["total_due"] for r in rows), 2)
    partial_inv = sum(r["partial_invoices"] for r in rows)
    partial_due = round(sum(r["partial_due"] for r in rows), 2)

    lines = [
        f"# Top {len(top)} des clients debiteurs - {run_date.isoformat()} (vue facture API v2 croisee compte 411)",
        "",
        "POC lecture seule : aucun envoi effectue, aucune donnee modifiee.",
        "",
        f"- Clients debiteurs (au moins une facture echue non soldee) : **{len(rows)}**",
        f"- Reste du total sur factures echues : **{total_due:.2f} EUR**",
        f"- Dont paiement partiel (prelevement automatique, HORS relances) : {partial_inv} factures, {partial_due:.2f} EUR",
        "",
        "| Rang | Client | Email | Factures en retard | Reste du (EUR) | Solde compte 411 | Ecart (411 - factures) |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(top, 1):
        bal = f"{r['balance_411']:.2f}" if r["balance_411"] is not None else "—"
        gap = f"{r['gap_411_minus_invoices']:.2f}" if r["gap_411_minus_invoices"] is not None else "—"
        lines.append(f"| {i} | {r['customer_name']} | {r['customer_email'] or ''} | {r['invoices_overdue']} | {r['total_due']:.2f} | {bal} | {gap} |")
    lines += [
        "",
        "Lecture : 'Reste du' = somme des factures echues non soldees (API v2, meme base que l'app",
        "Pennylane) ; 'Solde compte 411' = solde comptable du compte client (peut contenir des",
        "ecritures hors factures) ; l'ecart explique les differences entre l'app et la comptabilite.",
    ]
    return {
        "today": run_date.isoformat(),
        "debtor_count": len(rows),
        "total_due": total_due,
        "partial_payment_excluded_from_relance": {"invoices": partial_inv, "due": partial_due},
        "top": top,
        "report_markdown": "\n".join(lines),
    }


def _norm_name(s: Any) -> str:
    return " ".join(str(s or "").upper().split())


@agents.tool
def fetch_top_debtors(company_id: int, today: str | None = None, top_n: int = 20, max_pages: int = 40) -> dict[str, Any]:
    """Top N des clients debiteurs, calcule ENTIEREMENT cote serveur en un seul appel outil.

    company_id : identifiant Pennylane (ex. 21994007). today : date du jour YYYY-MM-DD
        (vide = date courante). top_n : taille du top (defaut 20). max_pages : securite
        pagination factures (defaut 40 pages de 100).

    Appels API v2 effectues en GET uniquement (token lu dans l'environnement du worker,
    jamais passe en argument ni affiche) :
    - GET /customers (noms/emails) ;
    - GET /ledger_accounts?filter=[number start_with "411"] (limit 1000 ; repli sur un
      appel non filtre + filtre local si le filtre est refuse ; si le scope
      ledger_accounts:readonly est absent -> 403, accounts_411=false). ATTENTION : l'API v2
      ne renvoie AUCUN solde sur ledger_accounts (pas de champ total_balance) -> on ne
      recupere que les numeros de comptes 411 ; les soldes 411 sont lus via MCP
      list_accounts (voir compute_encours_report) ;
    - GET /customer_invoices?filter=[draft=false, credit_note=false], pagine par cursor,
      agrege A LA VOLEE par client (aucune grosse donnee ne transite par l'agent) :
      nb de factures en retard (echeance <= today, reste du > 0) et total du reste du,
      croises avec les numeros du/des compte(s) 411 dont le label = nom du client.

    Retourne un dict : debtor_count, total_due, accounts_411, source="pennylane_api_v2",
    top (liste triee par reste du decroissant) et report_markdown (tableau markdown).
    Les factures en paiement partiel (amount_paid > 0) sont comptabilisees a part dans
    partial_payment_excluded_from_relance : elles restent dans l'encours mais sont HORS
    relances (prelevement automatique).
    """
    if not isinstance(company_id, int) or company_id <= 0:
        raise agents.ToolError("company_id doit etre un entier positif (ex. 21994007).")
    if today:
        run_date = _parse_date(today)
        if run_date is None:
            raise agents.ToolError("today doit etre au format YYYY-MM-DD.")
    else:
        run_date = agents.now().date()
    if not isinstance(top_n, int) or top_n <= 0:
        raise agents.ToolError("top_n doit etre un entier positif.")

    cust_by_id: dict[int, dict[str, Any]] = {}
    try:
        cursor = None
        for _ in range(20):
            data = _api_v2_get("/customers", {"limit": 100, "cursor": cursor}, company_id)
            for c in data.get("items", []):
                if c.get("id") is not None:
                    cust_by_id[c["id"]] = c
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            time.sleep(0.3)
    except agents.ToolError:
        cust_by_id = {}

    acc_by_name: dict[str, dict[str, Any]] = {}
    accounts_411 = False
    acc_filter = json.dumps([{"field": "number", "operator": "start_with", "value": "411"}])

    def _read_ledger_accounts(params: dict[str, Any]) -> None:
        cursor = None
        for _ in range(30):
            data = _api_v2_get("/ledger_accounts", {**params, "cursor": cursor}, company_id)
            for a in data.get("items", []):
                if not str(a.get("number") or "").startswith("411"):
                    continue
                key = _norm_name(a.get("label"))
                if not key:
                    continue
                acc_by_name.setdefault(key, {"numbers": []})["numbers"].append(a.get("number"))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            time.sleep(0.3)

    try:
        try:
            _read_ledger_accounts({"limit": 1000, "filter": acc_filter})
        except agents.ToolError:
            acc_by_name.clear()
            _read_ledger_accounts({"limit": 1000})
        accounts_411 = True
    except agents.ToolError:
        accounts_411 = False

    debtors: dict[str, dict[str, Any]] = {}
    invoices_seen = 0
    pages = 0
    cursor = None
    flt = json.dumps([
        {"field": "draft", "operator": "eq", "value": "false"},
        {"field": "credit_note", "operator": "eq", "value": "false"},
    ])
    while pages < max_pages:
        data = _api_v2_get("/customer_invoices", {"limit": 100, "cursor": cursor, "filter": flt, "sort": "-id"}, company_id)
        pages += 1
        for item in data.get("items", []):
            invoices_seen += 1
            mapped = _map_v2_invoice(item, cust_by_id)
            if mapped is None:
                continue
            deadline = _parse_date(mapped.get("deadline"))
            if deadline is None or deadline > run_date:
                continue
            amount_due = round(_to_float(mapped.get("amount")) - _to_float(mapped.get("amount_paid")), 2)
            if amount_due <= 0.005:
                continue
            name = str(mapped.get("customer_name") or "client inconnu")
            key = _norm_name(name)
            cur = debtors.setdefault(key, {
                "customer_name": name,
                "customer_email": mapped.get("customer_email"),
                "invoices_overdue": 0,
                "total_due": 0.0,
                "partial_invoices": 0,
                "partial_due": 0.0,
                "currency": mapped.get("currency") or "EUR",
                "non_eur": False,
            })
            if (mapped.get("currency") or "EUR") != cur["currency"]:
                cur["non_eur"] = True
            cur["invoices_overdue"] += 1
            cur["total_due"] = round(cur["total_due"] + amount_due, 2)
            if _to_float(mapped.get("amount_paid")) > 0.005:
                cur["partial_invoices"] += 1
                cur["partial_due"] = round(cur["partial_due"] + amount_due, 2)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(0.3)

    rows = []
    for key, d in debtors.items():
        acc = acc_by_name.get(key)
        rows.append({
            "customer_name": d["customer_name"],
            "customer_email": d["customer_email"],
            "invoices_overdue": d["invoices_overdue"],
            "total_due": d["total_due"],
            "partial_invoices": d["partial_invoices"],
            "partial_due": d["partial_due"],
            "currency": "EUR" if not d["non_eur"] else d["currency"] + " (mixte)",
            "balance_411": None,
            "accounts_411": acc["numbers"] if acc else [],
            "gap_411_minus_invoices": None,
        })
    rows.sort(key=lambda r: -r["total_due"])
    top = rows[:top_n]
    total_due = round(sum(r["total_due"] for r in rows), 2)
    partial_inv = sum(r["partial_invoices"] for r in rows)
    partial_due = round(sum(r["partial_due"] for r in rows), 2)

    lines = [
        f"# Top {len(top)} des clients debiteurs - {run_date.isoformat()} (API v2, {invoices_seen} factures lues, {pages} pages)",
        "",
        "POC lecture seule : aucun envoi effectue, aucune donnee modifiee.",
        "",
        f"- Clients debiteurs (au moins une facture echue non soldee) : **{len(rows)}**",
        f"- Reste du total sur factures echues : **{total_due:.2f} EUR**",
        f"- Dont paiement partiel (prelevement automatique, HORS relances) : {partial_inv} factures, {partial_due:.2f} EUR",
        f"- Comptes 411 identifies via API v2 : {'oui' if accounts_411 else 'non (scope ledger_accounts:readonly absent ou filtre refuse -> colonne 411 vide)'}",
        "",
        "| Rang | Client | Email | Factures en retard | Reste du (EUR) | Comptes 411 |",
        "|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(top, 1):
        accs = ", ".join(r["accounts_411"]) if r["accounts_411"] else "—"
        lines.append(f"| {i} | {r['customer_name']} | {r['customer_email'] or ''} | {r['invoices_overdue']} | {r['total_due']:.2f} | {accs} |")
    lines += [
        "",
        "Lecture : 'Reste du' = somme des factures echues non soldees (API v2, meme base que l'app",
        "Pennylane). L'API v2 ne renvoie pas le solde des comptes du grand livre (ledger_accounts",
        "sans total_balance) : le solde comptable 411 de chaque client se lit via MCP list_accounts",
        "(voir compute_encours_report) ; il peut contenir des ecritures hors factures, d'ou des",
        "ecarts entre l'encours de l'app et la comptabilite.",
    ]
    return {
        "today": run_date.isoformat(),
        "source": "pennylane_api_v2",
        "invoices_seen": invoices_seen,
        "pages_fetched": pages,
        "accounts_411": accounts_411,
        "debtor_count": len(rows),
        "total_due": total_due,
        "partial_payment_excluded_from_relance": {"invoices": partial_inv, "due": partial_due},
        "top": top,
        "report_markdown": "\n".join(lines),
    }


def _norm_key(s: Any) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(s or "")) if not unicodedata.combining(c)).upper()


@agents.tool
def fetch_pennylane_email_templates(company_id: int, max_pages: int = 10) -> dict[str, Any]:
    """Sonde LECTURE SEULE (GET) des modeles d'e-mail de facturation Pennylane (API v2).

    company_id : identifiant Pennylane (ex. 21994007), transmis en header X-Company-Id.
    max_pages : securite pagination par chemin sonde (defaut 10 pages de 100).

    Sonde les chemins candidats /email_templates, /billing_email_templates et
    /customer_invoice_templates. Pour chacun, retourne : status ("ok" ou message
    d'erreur HTTP sanitize, jamais de token), count et items {id, name, kind, ...}.
    Sert a verifier que les modeles "Relance impayee j+15 / j+30 / j+45" existent et a
    recuperer leur id (email_template_id pour POST /customer_invoices/{id}/send_by_email
    en phase 2). Aucun envoi, aucune modification.

    Retourne un dict : source="pennylane_api_v2", results (par chemin),
    relance_templates_found (liste {path, id, name, kind}) et report_markdown.
    """
    if not isinstance(company_id, int) or company_id <= 0:
        raise agents.ToolError("company_id doit etre un entier positif (ex. 21994007).")
    results: dict[str, Any] = {}
    for path in ("/email_templates", "/billing_email_templates", "/customer_invoice_templates"):
        items: list[dict[str, Any]] = []
        status = "ok"
        try:
            cursor = None
            for _ in range(max_pages):
                data = _api_v2_get(path, {"limit": 100, "cursor": cursor}, company_id)
                items.extend(data.get("items", []))
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
                time.sleep(0.3)
        except agents.ToolError as e:
            status = str(e)
        results[path] = {"status": status, "count": len(items), "items": items[:100]}

    wanted = {_norm_key(v) for v in PHASE2_CONFIG["relance_email_templates"].values()}
    found = []
    for path, r in results.items():
        for it in r["items"]:
            blob = _norm_key(" ".join(str(v) for v in it.values() if isinstance(v, str)))
            if any(w in blob for w in wanted):
                found.append({
                    "path": path,
                    "id": it.get("id"),
                    "name": it.get("name") or it.get("label") or it.get("title"),
                    "kind": it.get("kind"),
                    "raw": it,
                })

    lines = [
        f"# Modeles d'e-mail Pennylane - sonde API v2 (company_id {company_id})",
        "",
        "POC lecture seule : aucun envoi effectue, aucune donnee modifiee.",
        "",
    ]
    for path, r in results.items():
        lines.append(f"## `{path}` - statut : {r['status']} - {r['count']} modele(s)")
        if r["items"]:
            lines += ["", "| id | kind | autres champs |", "|---|---|---|"]
            for it in r["items"]:
                rest = {k: v for k, v in it.items() if k not in ("id", "kind")}
                lines.append(f"| {it.get('id')} | {it.get('kind') or ''} | {json.dumps(rest, ensure_ascii=False)[:400]} |")
        lines.append("")
    lines.append(f"Modeles de relance impayee trouves (j+15 / j+30 / j+45) : {len(found)}")
    for f in found:
        lines.append(f"- `{f['path']}` id={f['id']} name=\"{f['name']}\" kind={f.get('kind')}")
    return {
        "source": "pennylane_api_v2",
        "results": results,
        "relance_templates_found": found,
        "report_markdown": "\n".join(lines),
    }


@agents.tool
def fetch_relance_plan(company_id: int, today: str | None = None, journal_json: str = "[]", max_pages: int = 40) -> dict[str, Any]:
    """Plan de relance DU JOUR, calcule entierement cote serveur (API v2, GET uniquement) et filtre via le journal Drive.

    company_id : identifiant Pennylane (ex. 21994007). today : date du jour YYYY-MM-DD (vide = aujourd'hui).
    journal_json : contenu actuel du journal Drive (liste JSON d'entrees {invoice_id, stage, channel, at,
        dry_run}) : toute facture dont le couple (invoice_id, stage) y figure deja (hors entrees dry_run)
        est exclue du plan. max_pages : securite pagination factures (defaut 40 pages de 100).

    Appels effectues (GET uniquement ; token lu dans l'environnement du worker, jamais argument ni affiche) :
    GET /me (verification best effort du scope customer_invoices:all), GET /customers, GET /customer_invoices
    pagine par cursor. Le plan est calcule par compute_relance_report (memes regles metier deterministes :
    J = echeance, paliers J+15/30/45/60/90, exclusion 1re relance si emission = echeance, paiement partiel
    hors relances).

    Retourne : today, source, scopes_ok, invoices_seen, pages_fetched, by_stage, total_due, plan (factures
    a traiter, chacune avec action : "pennylane_email" niveaux 1-3, "gmail_draft" niveau 4 J+60,
    "drive_letter" niveau 5 J+90), excluded_by_journal et report_markdown. Aucune ecriture, aucun envoi.
    """
    if not isinstance(company_id, int) or company_id <= 0:
        raise agents.ToolError("company_id doit etre un entier positif (ex. 21994007).")
    if today:
        run_date = _parse_date(today)
        if run_date is None:
            raise agents.ToolError("today doit etre au format YYYY-MM-DD.")
    else:
        run_date = agents.now().date()
    journal = _parse_journal(journal_json)
    if not isinstance(max_pages, int) or max_pages <= 0:
        raise agents.ToolError("max_pages doit etre un entier positif.")

    scopes_ok = None
    try:
        me_blob = json.dumps(_api_v2_get("/me", {}, company_id), ensure_ascii=False)
        scopes_ok = "customer_invoices:all" in me_blob
    except agents.ToolError:
        scopes_ok = None

    try:
        cust_by_id = _fetch_v2_customers(company_id)
    except agents.ToolError:
        cust_by_id = {}
    invoices, pages, seen = _fetch_v2_invoices(company_id, cust_by_id, max_pages)

    report = compute_relance_report(json.dumps(invoices, ensure_ascii=False), today=run_date.isoformat())
    action_by_level = {1: "pennylane_email", 2: "pennylane_email", 3: "pennylane_email", 4: "gmail_draft", 5: "drive_letter"}
    plan: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for e in report["items"]:
        level = e["stage"]["level"]
        if _journal_has(journal, e["invoice_id"], level):
            excluded.append({"invoice_id": e["invoice_id"], "number": e["number"], "customer_name": e["customer_name"], "stage": level})
            continue
        e["action"] = action_by_level[level]
        plan.append(e)
    plan.sort(key=lambda e: (-e["stage"]["level"], -e["days_late"]))
    by_stage: dict[str, int] = {}
    for e in plan:
        by_stage[str(e["stage"]["level"])] = by_stage.get(str(e["stage"]["level"]), 0) + 1
    total_due = round(sum(e["amount_due"] for e in plan), 2)
    currency = plan[0]["currency"] if plan else "EUR"

    lines = [
        f"# Plan de relance du {run_date.isoformat()} - company_id {company_id} (API v2, {seen} factures lues, {pages} pages)",
        "",
        f"- Scope customer_invoices:all (GET /me) : {'OK' if scopes_ok else ('INDISPONIBLE' if scopes_ok is None else 'ABSENT (les envois reels seront refuses)')}",
        f"- Factures en retard avec action prevue : **{len(plan)}** (reste du **{total_due:.2f} {currency}**)",
        f"- Deja relancees selon le journal (exclues du plan) : {len(excluded)}",
        f"- Par palier : {json.dumps(by_stage, ensure_ascii=False)}",
        "",
    ]
    if plan:
        lines += [
            "| Facture | Client | Email | Reste du | Echeance (J) | Retard (j) | Palier | Action |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for e in plan:
            s = e["stage"]
            lines.append(
                f"| {e['number'] or e['invoice_id']} | {e['customer_name']} | {e['customer_email'] or ''} | "
                f"{e['amount_due']:.2f} {e['currency']} | {e['deadline']} | {e['days_late']} | "
                f"{s['level']} ({s['label']}) | {e['action']} |"
            )
        lines.append("")
    if excluded:
        lines += ["## Exclues (deja dans le journal)", ""]
        for x in excluded:
            lines.append(f"- {x['number'] or x['invoice_id']} ({x['customer_name']}) - palier {x['stage']}")
        lines.append("")

    return {
        "today": run_date.isoformat(),
        "company_id": company_id,
        "source": "pennylane_api_v2",
        "scopes_ok": scopes_ok,
        "invoices_seen": seen,
        "pages_fetched": pages,
        "by_stage": by_stage,
        "total_due": total_due,
        "currency": currency,
        "plan": plan,
        "excluded_by_journal": excluded,
        "report_markdown": "\n".join(lines),
    }


@agents.tool
def send_pennylane_invoice_email(company_id: int, invoice_id: int, stage: int, dry_run: bool | None = None, journal_json: str = "[]") -> dict[str, Any]:
    """Envoie la relance e-mail Pennylane d'une facture (POST /customer_invoices/{id}/send_by_email) ou la simule en dry-run.

    company_id : identifiant Pennylane. invoice_id : id interne API v2 de la facture. stage : palier 1 (J+15),
        2 (J+30) ou 3 (J+45) uniquement (J+60 = brouillon Gmail, J+90 = lettre Drive). dry_run : true = aucun
        POST effectue (defaut = env RELANCE_DRY_RUN, "true" par securite) ; false = envoi reel. journal_json :
        journal Drive actuel (anti-doublon + plafond quotidien).

    Validations cote serveur avant tout envoi : facture existante et non soldee, sans paiement partiel
    (prelevement automatique), echeance passee, palier recalcule == stage demande (J = echeance ; exclusion
    1re relance si emission = echeance), couple (invoice_id, stage) absent du journal, scope
    customer_invoices:all verifie via GET /me (envoi reel uniquement), plafond quotidien RELANCE_DAILY_SEND_CAP
    (defaut 20, envoi reel uniquement), modele d'email du palier retrouve par son nom dans
    GET /billing_email_templates. Envoi : POST /customer_invoices/{id}/send_by_email avec
    {"email_template_id": id} (recipients laisse vide = e-mails du client dans Pennylane).
    204 = e-mail en file d'envoi ; 409 = PDF pas encore genere (reessayer dans quelques minutes).

    Retourne : sent, dry_run, status_http, template_id, invoice_id, stage, customer_email, journal_entry
    (a ajouter au journal) et journal_json (journal mis a jour, pret a etre reecrit dans Drive).
    Le token n'est jamais un argument ni affiche.
    """
    if not isinstance(company_id, int) or company_id <= 0:
        raise agents.ToolError("company_id doit etre un entier positif (ex. 21994007).")
    if not isinstance(invoice_id, int) or invoice_id <= 0:
        raise agents.ToolError("invoice_id doit etre un entier positif (id interne API v2 de la facture).")
    if stage not in (1, 2, 3):
        raise agents.ToolError("stage doit etre 1 (J+15), 2 (J+30) ou 3 (J+45). J+60 = brouillon Gmail, J+90 = lettre Drive.")
    dry = _dry_run_default() if dry_run is None else bool(dry_run)
    journal = _parse_journal(journal_json)
    today = agents.now().date()

    if _journal_has(journal, invoice_id, stage):
        raise agents.ToolError(f"Facture {invoice_id} palier {stage} : deja relancee selon le journal (anti-doublon).")

    inv = _api_v2_get(f"/customer_invoices/{invoice_id}", {}, company_id)
    status = inv.get("status") or ""
    if status in SKIP_STATUSES or inv.get("draft") or inv.get("credit_note"):
        raise agents.ToolError(f"Facture {invoice_id} : statut {status or 'inconnu'} non relancable (brouillon/avoir/annulee).")
    amount = _to_float(inv.get("currency_amount") or inv.get("amount"))
    remaining = inv.get("remaining_amount_with_tax")
    amount_paid = amount if inv.get("paid") else (round(amount - _to_float(remaining), 2) if remaining is not None else 0.0)
    amount_due = round(amount - amount_paid, 2)
    if amount_due <= 0.005:
        raise agents.ToolError(f"Facture {invoice_id} deja soldee (reste du {amount_due:.2f}) : aucune relance.")
    if amount_paid > 0.005:
        raise agents.ToolError(
            f"Facture {invoice_id} en paiement partiel ({amount_paid:.2f} paye, {amount_due:.2f} du) : "
            "jamais relancee, le solde est encaisse par prelevement automatique."
        )
    issue = _parse_date(inv.get("date"))
    deadline = _parse_date(inv.get("deadline"))
    stage_calc, days_late = _stage_for(deadline, issue, today)
    if stage_calc is None:
        if deadline is None:
            reason = "pas de date d'echeance"
        elif days_late < 0:
            reason = f"non echue (echeance {deadline.isoformat()})"
        elif issue is not None and issue == deadline:
            reason = "exclusion 1re relance : date d'emission = date d'echeance"
        else:
            reason = f"hors fenetre de relance ({days_late} j de retard)"
        raise agents.ToolError(f"Facture {invoice_id} : aucun palier applicable aujourd'hui ({reason}).")
    if stage_calc["level"] != stage:
        raise agents.ToolError(f"Facture {invoice_id} : palier attendu {stage_calc['level']} ({days_late} j de retard), pas {stage}.")
    customer = inv.get("customer") or {}
    customer_email = None
    if isinstance(customer.get("emails"), list) and customer["emails"]:
        customer_email = customer["emails"][0]
    elif customer.get("email"):
        customer_email = customer["email"]

    template = _resolve_template_id(company_id, stage)
    if template is None:
        msg = (
            f"Modele d'email du palier {stage} (\"{PHASE2_CONFIG['relance_email_templates'][stage]}\") absent de "
            "GET /billing_email_templates : a creer dans l'UI Pennylane avant tout envoi reel."
        )
        if dry:
            return {"sent": False, "dry_run": True, "invoice_id": invoice_id, "stage": stage, "template_id": None,
                    "customer_email": customer_email, "warning": msg, "journal_entry": None, "journal_json": journal_json}
        raise agents.ToolError(msg)

    if dry:
        entry = {"invoice_id": invoice_id, "stage": stage, "channel": "pennylane_email", "template_id": template.get("id"),
                 "at": agents.now().isoformat(), "dry_run": True}
        return {"sent": False, "dry_run": True, "status_http": None, "template_id": template.get("id"),
                "invoice_id": invoice_id, "stage": stage, "customer_email": customer_email,
                "journal_entry": entry, "journal_json": json.dumps(journal + [entry], ensure_ascii=False)}

    me_blob = json.dumps(_api_v2_get("/me", {}, company_id), ensure_ascii=False)
    if "customer_invoices:all" not in me_blob:
        raise agents.ToolError(
            "Scope customer_invoices:all ABSENT du token (GET /me) : envoi reel refuse. Regenerer la cle API "
            "Pennylane avec ce scope et mettre a jour le secret workspace cle_api_pennylane."
        )
    cap = _daily_send_cap()
    if _journal_today_email_sends(journal, today.isoformat()) >= cap:
        raise agents.ToolError(f"Plafond quotidien atteint ({cap} envois/jour, env {RELANCE_DAILY_CAP_ENV}) : aucune relance supplementaire aujourd'hui.")

    status_http, payload = _api_v2_post(
        f"/customer_invoices/{invoice_id}/send_by_email", {}, company_id,
        {"email_template_id": template.get("id")},
    )
    if status_http == 409:
        raise agents.ToolError(f"Facture {invoice_id} : HTTP 409, PDF pas encore genere cote Pennylane -> reessayer dans quelques minutes.")
    if status_http >= 400:
        body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)[:300]
        raise agents.ToolError(f"send_by_email facture {invoice_id} -> HTTP {status_http}: {body}")
    entry = {"invoice_id": invoice_id, "stage": stage, "channel": "pennylane_email", "template_id": template.get("id"),
             "at": agents.now().isoformat(), "dry_run": False}
    return {"sent": status_http in (200, 202, 204), "dry_run": False, "status_http": status_http,
            "template_id": template.get("id"), "invoice_id": invoice_id, "stage": stage,
            "customer_email": customer_email, "journal_entry": entry,
            "journal_json": json.dumps(journal + [entry], ensure_ascii=False)}


@agents.tool
def fetch_invoice_pdf_base64(company_id: int, invoice_id: int, max_bytes: int = PDF_MAX_BYTES) -> dict[str, Any]:
    """Telecharge le PDF d'une facture Pennylane (via public_file_url, lien signe a courte validite) pour la PJ du brouillon Gmail J+60.

    company_id : identifiant Pennylane. invoice_id : id interne API v2 de la facture. max_bytes : taille max
        acceptee (defaut 3 000 000, limite PJ d'un brouillon Gmail).
    Retourne {too_large: false, filename, mime_type: "application/pdf", size, base64} si size <= max_bytes,
    sinon {too_large: true, size, max_bytes, filename, drive_link_hint} (deposer le PDF dans le dossier Drive
    et mettre son lien dans le corps du brouillon). Lecture seule ; le token reste dans l'env du worker.
    """
    if not isinstance(company_id, int) or company_id <= 0:
        raise agents.ToolError("company_id doit etre un entier positif (ex. 21994007).")
    if not isinstance(invoice_id, int) or invoice_id <= 0:
        raise agents.ToolError("invoice_id doit etre un entier positif (id interne API v2 de la facture).")
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise agents.ToolError("max_bytes doit etre un entier positif.")
    inv = _api_v2_get(f"/customer_invoices/{invoice_id}", {}, company_id)
    url = inv.get("public_file_url")
    if not url:
        raise agents.ToolError(f"Facture {invoice_id} : aucun public_file_url (PDF probablement pas encore genere).")
    data = _download_bytes(url, max_bytes)
    number = inv.get("invoice_number") or inv.get("label") or str(invoice_id)
    if len(data) > max_bytes:
        return {
            "too_large": True,
            "size": len(data),
            "max_bytes": max_bytes,
            "filename": f"{number}.pdf",
            "drive_link_hint": "PDF trop gros pour une PJ Gmail : deposer le PDF dans le dossier Drive Relance-factures et mettre son lien dans le corps du brouillon.",
        }
    return {
        "too_large": False,
        "filename": f"{number}.pdf",
        "mime_type": "application/pdf",
        "size": len(data),
        "base64": base64.b64encode(data).decode("ascii"),
    }


GMAIL_ALLOWED = {"gmail_create_draft", "create_draft", "gmail_search_threads", "search_threads", "gmail_get_thread", "get_thread"}
GMAIL_DENY_HINTS = ("send", "delete")
DRIVE_ALLOWED = {
    "google_drive_mcp_search_files", "search_files",
    "google_drive_mcp_read_file_content", "read_file_content",
    "google_drive_mcp_download_file_content", "download_file_content",
    "google_drive_mcp_list_recent_files", "list_recent_files",
    "google_drive_mcp_get_file_metadata", "get_file_metadata",
    "google_drive_mcp_create_file", "create_file",
    "google_drive_mcp_update_file", "update_file",
    "google_drive_mcp_copy_file", "copy_file",
}
DRIVE_DENY_HINTS = ("delete", "trash", "permission", "share", "move", "empty")


class RelanceGuardrail(agents.Hook):
    """Phase 2 (v9) : n'autorise que client.*, les lectures Pennylane, gmail_create_draft (+ lectures Gmail) et les lectures/creations/mises a jour Drive.

    Bloque : run_operations (remplace par les outils client.* qui appellent l'API v2 directement), tout
    envoi Gmail (gmail_send_email, gmail_send_draft), les suppressions/labels Gmail, les suppressions et
    la gestion des permissions Drive, run_typescript et tout autre connecteur ou outil inconnu.
    """

    async def pre_tool_call(self, ctx, call):
        tool_id = call.tool_id or ""
        name = call.tool_name or ""
        candidates = {name, tool_id, tool_id.split(".")[-1]}
        if tool_id.startswith("client."):
            return None
        if tool_id.startswith(PENNYLANE_PREFIX):
            if name in CURATED_READ_TOOLS or name.startswith(READ_PREFIXES):
                if name == "run_operations":
                    return agents.skip(
                        "run_operations est bloque en phase 2 : utiliser les outils client.* (fetch_relance_plan, "
                        "send_pennylane_invoice_email, fetch_invoice_pdf_base64) qui appellent l'API v2 directement."
                    )
                return None
            return agents.skip(
                f"L'outil Pennylane '{name}' n'est pas autorise. Utilise les lectures list_*/get_*/query_* "
                "ou les outils client.*."
            )
        if tool_id.startswith(GMAIL_PREFIX):
            if (candidates & GMAIL_ALLOWED) and not any(h in name.lower() for h in GMAIL_DENY_HINTS):
                return None
            return agents.skip(
                f"Outil Gmail '{name}' bloque : seul gmail_create_draft (plus lectures search/get) est autorise. "
                "Aucun envoi Gmail automatique (gmail_send_email / gmail_send_draft bloques)."
            )
        if tool_id.startswith(DRIVE_PREFIX):
            if any(h in name.lower() for h in DRIVE_DENY_HINTS):
                return agents.skip(f"Outil Drive '{name}' bloque : suppressions et gestion des permissions interdites.")
            if candidates & DRIVE_ALLOWED:
                return None
            return agents.skip(
                f"Outil Drive '{name}' non autorise : lectures, create_file, update_file et copy_file uniquement "
                f"(journal {JOURNAL_FILENAME}, rapports, lettres de mise en demeure)."
            )
        return agents.skip(
            f"L'outil '{name}' n'est pas autorise : cet agent n'utilise que les outils client.*, le connecteur "
            "Pennylane en lecture seule, gmail_create_draft et le connecteur Drive (lecture/creation/mise a jour)."
        )


INSTRUCTIONS = """Tu es le responsable des encaissements de l'agence. Tu reponds en francais.

OBJECTIF DE CE POC (100% LECTURE SEULE) : produire un RAPPORT A BLANC du plan de relance
des factures clients en retard dans Pennylane (company_id fourni par l'utilisateur).
Tu n'envoies rien, tu ne crees rien, tu ne modifies rien : le garde-fou bloque toute
ecriture et tout outil hors connecteur Pennylane (lecture seule) + outils client.*.

SOURCE DE DONNEES PRINCIPALE - API Company v2 (le MCP renvoie des 500 sur les endpoints
factures pour cette entreprise, sonde du 2026-11-22) :
- L'outil client.fetch_pennylane_customer_invoices(company_id=<id>) appelle l'API v2 en
  GET uniquement (factures + clients), lit le token dans son environnement, pagine tout
  seul et retourne invoices_json directement utilisable. NE lui passe JAMAIS de token.

PROTOCOLE :
1. company_id : prends-le dans le message de l'utilisateur (nombre entier, ex.
   "company_id: 21994007") — NE PAS deviner.
2. Appelle fetch_pennylane_customer_invoices(company_id=<id>). Lis le resultat :
   invoices_count, pages_fetched, invoices_json.
3. Appelle compute_relance_report(invoices_json=<champ invoices_json du resultat de
   l'etape 2>, today=<YYYY-MM-DD du jour>). Ne calcule rien toi-meme.
4. Ta reponse finale est UNIQUEMENT le report_markdown retourne par
   compute_relance_report, suivi de la phrase "Aucun envoi effectue (POC lecture seule)."
5. Si l'utilisateur demande un TOP N des debiteurs : appelle UNIQUEMENT
   fetch_top_debtors(company_id=<id>, today=<YYYY-MM-DD>, top_n=N). Tout est calcule cote
   serveur dans l'outil (factures + clients + comptes 411 via API v2) : ne passe AUCUN gros
   JSON entre outils et ne pagine pas list_accounts toi-meme. Reponds avec son
   report_markdown + 'Aucun envoi effectue (POC lecture seule).'
6. Variante si fetch_top_debtors est indisponible : top_debtors_report(invoices_json=...,
   accounts_json=<comptes 411 MCP>, today=..., top_n=N).
7. Si l'utilisateur demande de VERIFIER les prerequis phase 2 (modeles d'email de
   relance Pennylane "Relance impayee j+15/j+30/j+45") : appelle UNIQUEMENT
   fetch_pennylane_email_templates(company_id=<id>) et reponds avec son
   report_markdown + 'Aucun envoi effectue (POC lecture seule).'

FALLBACK (uniquement si l'etape 2 echoue, ex. 401/403/429 ou reseau) :
a. list_accounts(company_id=<id>, number_prefix="411", page=1..N) en paginant
   (pagination.total ~932 comptes, 10 pages) ; b. list_customers(company_id=<id>,
   page=1..N, per_page=100) (~645 clients, 7 pages) ; c. list_customer_payments(
   company_id=<id>, page=1..N) (~131 paiements, 2 pages) ; d. compute_encours_report(
   accounts_json=..., customers_json=..., payments_json=..., today=...) ; e. reponse =
   report_markdown de compute_encours_report + "Aucun envoi effectue (POC lecture seule)."
   + une phrase indiquant que le plan facture par facture (J+15..J+90) n'a pas pu etre
   calcule car l'API v2 a echoue.
- NE TENTE PAS list_customer_invoices / get_customer_invoice / search_operations /
  run_operations / list_ledger_entry_lines non filtre : 500 ou "Operation inconnue" pour
  cette entreprise (sonde du 2026-11-22). Un seul essai maximum par outil en fallback.
- Outils MCP qui fonctionnent : get_company, list_customers, get_customer,
  list_customer_payments, list_accounts, get_changelog.

REGLES METIER (appliquees par l'outil compute_relance_report) :
- J = date d'echeance (date de paiement demande), pas la date d'emission.
- Seuils : J+15 1re relance (tres cordial), J+30 2e relance (courtois), J+45 3e relance
  (plus ferme), J+60 4e relance (ferme mais respectueux, email + appel cible),
  J+90 mise en demeure (formel, courrier recommande).
- Une facture dont la date d'emission est IDENTIQUE a sa date d'echeance n'est PAS
  sujette a la 1re relance.
- Ne jamais relancer une facture deja soldee (reste du = 0).
- Une facture ayant recu un PAIEMENT PARTIEL (deja paye > 0 et reste du > 0) n'est
  JAMAIS relancee, quel que soit le palier : le solde est encaisse par PRELEVEMENT
  AUTOMATIQUE (compte dans excluded_partial_payment, hors du plan).

ERREURS : si un outil renvoie une erreur, lis le message et explique clairement le
probleme. Ne devine jamais un montant ni une date. Ne jamais afficher de token/cles.

PROTOCOLE PHASE 2 (v9) - RUN QUOTIDIEN (dry-run par defaut, aucun envoi reel tant que
RELANCE_DRY_RUN n'est pas mis a "false" et que les prerequis ne sont pas remplis) :
1. company_id : prends-le dans le message de l'utilisateur (ex. "company_id: 21994007").
2. JOURNAL DRIVE (source de verite anti-doublon) : google_drive_mcp_search_files pour trouver
   "journal-relances.json" dans le dossier "Relance-factures" (id 1D3gWJnITWIpHHUKo13jgBClAmzWInDqP), puis
   google_drive_mcp_read_file_content -> journal_json. Si le fichier n'existe pas, journal_json = "[]".
3. PLAN : fetch_relance_plan(company_id=<id>, today=<YYYY-MM-DD>, journal_json=<journal_json>) -> plan
   (chaque item a une action : "pennylane_email" | "gmail_draft" | "drive_letter") + report_markdown.
4. Niveaux 1-3 (action "pennylane_email") : pour chaque item, send_pennylane_invoice_email(
   company_id=<id>, invoice_id=<item.invoice_id>, stage=<niveau 1/2/3>, dry_run=<dry_run demande>,
   journal_json=<journal courant>). Si le resultat contient journal_json mis a jour, REMPLACE le journal
   courant par cette valeur (l'outil a verifie anti-doublon, palier, scope, plafond, modele).
5. Niveau 4 J+60 (action "gmail_draft") : fetch_invoice_pdf_base64(company_id, invoice_id) puis
   gmail_create_draft(to=<customer_email>, subject="Facture <number> en attente de reglement - 4e rappel",
   body=..., attachments=[{filename, mimeType: "application/pdf", content: <base64>}] si too_large=false,
   sinon lien Drive dans le corps). JAMAIS gmail_send_email / gmail_send_draft : brouillon uniquement,
   envoi manuel apres revue. Ajoute au journal une entree {invoice_id, stage: 4, channel: "gmail_draft", at, dry_run}.
6. Niveau 5 J+90 (action "drive_letter") : redige la lettre de mise en demeure en markdown (emetteur
   ID Interactive SAS, SIRET 490491594, Arradon ; facture, montant, echeance, penalites de retard CGVS
   art. 8 : 3x taux legal + 40 EUR) puis google_drive_mcp_create_file(title="Mise en demeure - <client> -
   <facture> - <date>.md", parent_id=<dossier Relance-factures ou sous-dossier "Mises en demeure">,
   text_content=<lettre>, content_mime_type="text/markdown", disable_conversion_to_google_type=true).
   Envoi postal LRAR MANUEL : la lettre n'est jamais envoyee automatiquement. Journal : channel "drive_letter".
7. PERSISTE le journal final : google_drive_mcp_update_file sur le fichier journal (ou create_file s'il
   n'existait pas) avec le journal_json final. Puis ecris le rapport du jour :
   google_drive_mcp_create_file("rapport-<YYYY-MM-DD>.md" dans le sous-dossier "rapports", text_content=
   report_markdown + resume des actions).
8. Reponse finale = resume markdown : nb actions par palier, envois (ou simulations dry-run), brouillons
   crees, lettres redigees, lien vers le rapport Drive.

SECURITES (garde-fou RelanceGuardrail) : ne JAMAIS appeler gmail_send_email, gmail_send_draft,
run_operations, ni aucun outil de suppression/permission Drive - ils sont bloques. dry_run=true par defaut :
les envois Pennylane reels exigent dry_run=false explicite + scope customer_invoices:all + les 3 modeles
d'email j+15/j+30/j+45 dans Pennylane + plafond RELANCE_DAILY_SEND_CAP (defaut 20/jour).
"""

agent = agents.Agent(
    model="mistral-large-4",
    name="relance-factures-poc",
    description="Agent de relance des factures clients impayees Pennylane : plan deterministe J+15/30/45/60/90, envois e-mail Pennylane (dry-run par defaut), brouillon Gmail avec PDF pour J+60, lettre de mise en demeure dans Drive pour J+90, journal Drive anti-doublon.",
    instructions=INSTRUCTIONS,
    tools=[compute_relance_report, compute_encours_report, fetch_pennylane_customer_invoices, top_debtors_report, fetch_top_debtors, fetch_pennylane_email_templates, fetch_relance_plan, send_pennylane_invoice_email, fetch_invoice_pdf_base64],
    connectors=[agents.connector("pennylane_e376"), agents.connector("gmail"), agents.connector("google_drive_mcp")],
    builtins=agents.Builtins(skills=False, subagents=False),
    guardrails=[RelanceGuardrail()],
    max_iterations=60,
)
