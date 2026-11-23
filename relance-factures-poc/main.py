"""POC lecture seule : rapport a blanc du plan de relance des factures clients Pennylane.

L'agent lit les factures clients (dates d'emission, echeances, paiements, clients) :
- via l'API Company v2 (outil fetch_pennylane_customer_invoices, token lu depuis la
  variable d'environnement PENNYLANE_API_TOKEN injectee par le deploiement a partir du
  secret workspace ; le token n'est jamais un argument ni affiche), quand le MCP
  renvoie des 500 sur les endpoints factures ;
- via le connecteur MCP Pennylane en secours (comptes 411, clients, paiements).
Il applique les regles de relance (J = date d'echeance ; seuils J+15 / J+30 / J+45 /
J+60 / J+90 ; exclusion de la 1re relance quand date d'emission = date d'echeance ;
exclusion des factures en paiement partiel, encaissees par prelevement automatique) de
facon deterministe via l'outil compute_relance_report, et produit un rapport markdown.
Aucun envoi, aucune ecriture : le garde-fou bloque tout outil non autorise.
"""

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

WRITE_OP_HINTS = (
    "send", "post", "create", "update", "delete", "mark", "finalize",
    "import", "link", "letter", "upload", "put", "patch", "reminder",
)

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


def _api_v2_get(path: str, params: dict[str, Any], company_id: int) -> dict[str, Any]:
    """GET sur l'API Company v2 Pennylane. Le token vient UNIQUEMENT de l'env."""
    token = next(
        (os.environ[v].strip() for v in (API_TOKEN_ENV, API_TOKEN_ENV_FALLBACK) if os.environ.get(v, "").strip()),
        "",
    )
    if not token:
        raise agents.ToolError(
            f"Variable d'environnement {API_TOKEN_ENV} (ou {API_TOKEN_ENV_FALLBACK}) absente : "
            "le secret workspace (cle_api_pennylane) n'est pas injecte dans ce deploiement."
        )
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{PENNYLANE_API_BASE}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-Company-Id": str(company_id),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise agents.ToolError(f"API Pennylane v2 {path} -> HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise agents.ToolError(f"API Pennylane v2 {path} injoignable: {e.reason}")


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

    cust_by_id: dict[int, dict[str, Any]] = {}
    try:
        cursor = None
        for _ in range(20):
            params = {"limit": 100, "cursor": cursor}
            data = _api_v2_get("/customers", params, company_id)
            for c in data.get("items", []):
                if c.get("id") is not None:
                    cust_by_id[c["id"]] = c
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            time.sleep(0.3)
    except agents.ToolError:
        cust_by_id = {}

    invoices: list[dict[str, Any]] = []
    cursor = None
    pages = 0
    flt = json.dumps([
        {"field": "draft", "operator": "eq", "value": "false"},
        {"field": "credit_note", "operator": "eq", "value": "false"},
    ])
    while pages < max_pages:
        params = {"limit": 100, "cursor": cursor, "filter": flt, "sort": "-id"}
        data = _api_v2_get("/customer_invoices", params, company_id)
        pages += 1
        for item in data.get("items", []):
            mapped = _map_v2_invoice(item, cust_by_id)
            if mapped is not None:
                invoices.append(mapped)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(0.3)

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


class ReadOnlyPennylaneGuardrail(agents.Hook):
    """N'autorise que les outils Pennylane curates en lecture seule et les outils client.*.

    Autorise tout outil Pennylane nomme list_*, get_* ou query_* (allow-list lecture seule
    cote serveur) plus search_operations ; run_operations n'est autorise que si son
    operation_id ne ressemble pas a une ecriture. Bloque tout le reste (run_typescript,
    autres connecteurs, outils inconnus) : le POC est 100% lecture seule, aucun envoi ni
    aucune modification n'est possible.
    """

    async def pre_tool_call(self, ctx, call):
        if call.tool_id.startswith("client."):
            return None
        if call.tool_id.startswith(PENNYLANE_PREFIX):
            name = call.tool_name
            if name in CURATED_READ_TOOLS or name.startswith(READ_PREFIXES):
                if name == "run_operations":
                    args = call.arguments or {}
                    op = str(args.get("operation_id", ""))
                    if any(h in op.lower() for h in WRITE_OP_HINTS):
                        return agents.skip(
                            f"Operation '{op}' bloquee : ce POC est en lecture seule, "
                            "aucune operation d'ecriture (envoi, creation, modification) n'est autorisee."
                        )
                return None
            return agents.skip(
                f"L'outil Pennylane '{name}' n'est pas autorise dans ce POC (lecture seule). "
                "Utilise les outils curates en lecture seule (list_*, get_*, query_*, "
                "search_operations) : list_customer_invoices, get_customer_invoice, "
                "list_customer_payments, list_customers, get_customer, get_company..."
            )
        return agents.skip(
            f"L'outil '{call.tool_name}' n'est pas autorise : cet agent POC n'utilise que le "
            "connecteur Pennylane en lecture seule et son outil de calcul de rapport. "
            "Aucun envoi ni aucune ecriture n'est possible."
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
"""

agent = agents.Agent(
    model="mistral-large-4",
    name="relance-factures-poc",
    description="POC lecture seule : lit les factures clients impayees dans Pennylane (connexion de l'utilisateur) et produit un rapport a blanc du plan de relance J+15/30/45/60/90. Aucun envoi.",
    instructions=INSTRUCTIONS,
    tools=[compute_relance_report, compute_encours_report, fetch_pennylane_customer_invoices, top_debtors_report, fetch_top_debtors, fetch_pennylane_email_templates],
    connectors=[agents.connector("pennylane_e376")],
    builtins=agents.Builtins(skills=False, subagents=False),
    guardrails=[ReadOnlyPennylaneGuardrail()],
    max_iterations=60,
)
