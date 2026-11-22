"""POC lecture seule : rapport a blanc du plan de relance des factures clients Pennylane.

L'agent lit les factures clients (dates d'emission, echeances, paiements, clients) via le
connecteur Pennylane, applique les regles de relance (J = date d'echeance ; seuils
J+15 / J+30 / J+45 / J+60 / J+90 ; exclusion de la 1re relance quand date d'emission =
date d'echeance) de facon deterministe via l'outil compute_relance_report, et produit un
rapport markdown. Aucun envoi, aucune ecriture : le garde-fou bloque tout outil non
autorise.
"""

import json
from datetime import date, datetime
from typing import Any

from mistralai import agents

PENNYLANE_PREFIX = "connector_pennylane_e376."

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
    Les factures soldees ou non encore echues sont exclues.

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
            "status": _pick(inv, "status"),
        }
        if amount_due <= 0.005:
            paid.append(entry)
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

OBJECTIF DE CE POC (100% LECTURE SEULE) : produire un RAPPORT A BLANC sur les creances
clients en retard dans Pennylane (company_id fourni par l'utilisateur). Tu n'envoies
rien, tu ne crees rien, tu ne modifies rien : le garde-fou bloque toute ecriture.

CONNECTEUR PENNYLANE (pennylane_e376) - LECTURE SEULE :
- Tous les outils curates nommes list_*, get_* ou query_* sont autorises (allow-list
  100% lecture seule cote serveur Pennylane).
- DIAGNOSTIC CONNU pour cette entreprise (sonde realisee le 2026-11-22) — certains
  endpoints renvoient une erreur 500 "Error calling tool from MCP server" :
  list_customer_invoices (des qu'il y a des resultats), get_customer_invoice,
  search_operations (toujours), list_transactions, query_trial_balance,
  list_ledger_entry_lines non filtre, get_context sans company_id.
  NE RETENTE PAS ces outils plus d'une fois chacun. NE PAS appeler run_operations avec
  des operation_id inventes ("getCustomerInvoices" -> "Opération inconnue").
- OUTILS QUI FONCTIONNENT (a utiliser) : get_company, list_customers, get_customer,
  list_customer_payments, list_accounts (number_prefix="411"), get_changelog.

PROTOCOLE :
1. company_id : prends-le dans le message de l'utilisateur (nombre entier, ex.
   "company_id: 21994007") — NE PAS deviner. Appelle get_company(company_id=<id>) pour
   confirmer le profil.
2. ENCOURS CLIENTS (niveau compte 411) : list_accounts(company_id=<id>,
   number_prefix="411", page=1) puis pagine (pagination.total, ~932 comptes, 10 pages)
   en gardant tous les comptes. Champs : number, label, total_balance (solde debiteur =
   creance restante), total_debits, total_credits. Une requete a la fois.
3. CLIENTS : list_customers(company_id=<id>, page=1, per_page=100) en paginant
   (pagination.total, ~645 clients, 7 pages). Champs : id, name, emails, ...
   Le rapprochement compte 411 <-> client se fait sur le NOM (label du compte =
   nom du client).
4. PAIEMENTS : list_customer_payments(company_id=<id>, page=1) en paginant
   (pagination.total, ~131 paiements, 2 pages). Champs : id, date, customer_id,
   customer_name, invoice_id, amount, currency, method, status.
5. OPTIONNEL (une seule fois) : teste list_ledger_entry_lines(company_id=<id>,
   account_filter="411", period_start="2026-01-01", page=1). Si 200, pagine jusqu'a
   ~20 pages et reconstruis les factures (lignes document_type=CustomerInvoice :
   description = numero de facture, date, amount>0 = debit, lettering_balance != 0 =
   non soldee) ; deadline estimee = date + payment_conditions du client (ex.
   "30_days" -> 30 jours, valeur par defaut 30) ; puis appelle compute_relance_report
   avec cette liste. Si 500, abandonne cette etape (les encours restent valides).
6. Appelle compute_encours_report(accounts_json=<liste JSON des comptes 411>,
   customers_json=<liste JSON des clients>, payments_json=<liste JSON des paiements>,
   today=<YYYY-MM-DD du jour>). Ne calcule rien toi-meme : laisse l'outil le faire.
7. Ta reponse finale est UNIQUEMENT le rapport markdown retourne par l'outil (champ
   report_markdown ; si l'etape 5 a reussi, prefere le report_markdown de
   compute_relance_report), suivi d'une phrase indiquant qu'aucun envoi n'a ete
   effectue (POC lecture seule).

REGLES METIER (appliquees par l'outil compute_relance_report) :
- J = date d'echeance (date de paiement demande), pas la date d'emission.
- Seuils : J+15 1re relance (tres cordial), J+30 2e relance (courtois), J+45 3e relance
  (plus ferme), J+60 4e relance (ferme mais respectueux, email + appel cible),
  J+90 mise en demeure (formel, courrier recommande).
- Une facture dont la date d'emission est IDENTIQUE a sa date d'echeance n'est PAS
  sujette a la 1re relance.
- Ne jamais relancer une facture deja soldee (reste du = 0).

ERREURS : si un outil renvoie une erreur, lis le message, corrige l'appel (pagination,
parametres) ou explique clairement le probleme. Ne devine jamais un montant ni une date.
"""

agent = agents.Agent(
    model="mistral-large-4",
    name="relance-factures-poc",
    description="POC lecture seule : lit les factures clients impayees dans Pennylane (connexion de l'utilisateur) et produit un rapport a blanc du plan de relance J+15/30/45/60/90. Aucun envoi.",
    instructions=INSTRUCTIONS,
    tools=[compute_relance_report, compute_encours_report],
    connectors=[agents.connector("pennylane_e376")],
    builtins=agents.Builtins(skills=False, subagents=False),
    guardrails=[ReadOnlyPennylaneGuardrail()],
    max_iterations=60,
)
