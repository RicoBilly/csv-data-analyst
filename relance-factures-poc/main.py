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
des factures clients en retard dans Pennylane. Tu n'envoies rien, tu ne crees rien, tu ne
modifies rien : le garde-fou bloque toute ecriture.

CONNECTEUR PENNYLANE (pennylane_e376) - LECTURE SEULE :
- Tous les outils curates nommes list_*, get_* ou query_* sont autorises (allow-list
  100% lecture seule cote serveur Pennylane) : notamment list_customer_invoices,
  get_customer_invoice, list_customer_payments, list_customers, get_customer,
  get_company, list_transactions, query_trial_balance.
- search_operations / run_operations : utilisables UNIQUEMENT en LECTURE. C'est le
  chemin NOMINAL pour les factures (voir etape 2) car les outils curates factures
  tombent en panne pour cette entreprise. run_operations n'est JAMAIS autorise pour
  envoyer, creer, modifier ou supprimer quoi que ce soit (le garde-fou bloque toute
  operation d'ecriture : send/post/create/update/delete/mark/finalize/...).
- search_operations est actuellement en panne (erreur 500 sur presque toutes les
  requetes, avec ou sans company_id) : NE L'UTILISE PAS, ou au plus 1 essai. Passe
  directement a run_operations avec les operation_id connus ci-dessous.
- NE PAS appeler get_context sans company_id (erreur 500).

PROTOCOLE :
1. company_id : si le message de l'utilisateur contient explicitement un company_id
   (nombre entier, ex. "company_id: 21994007"), utilise-le directement — NE PAS deviner.
   Appelle get_company(company_id=<id>) pour confirmer le profil (cet outil fonctionne).
2. FACTURES CLIENTS — contournement connu : pour cette entreprise, list_customer_invoices
   renvoie une erreur 500 sur les requetes NON FILTREES (trop volumineuses cote serveur ;
   un filtre ne matchant rien, ex. label="__vide__", repond OK avec total=0), et
   get_customer_invoice renvoie 500 lui aussi. search_operations est en panne (500) et
   run_operations exige des operation_id internes inconnus ("getCustomerInvoices" et
   "listCustomerInvoices" -> "Opération inconnue"). Strategie, dans l'ordre :
   a. D'abord la liste des clients (outil qui fonctionne) : list_customers(
      company_id=<id>, page=1, per_page=100) en paginant via pagination.total ->
      collecte TOUS les customer ids (et leurs emails, utiles pour le rapport).
   b. Teste UN filtre etroit sur les factures : list_customer_invoices(company_id=<id>,
      page=1, customer_ids=[<id du premier client de la liste>]).
      - Si reponse 200 avec des lignes : le filtrage par client fonctionne. Liste alors
        les factures PAR LOT de clients : customer_ids=[...jusqu'a 50 ids...], page=1,
        en paginant chaque lot (pagination.total / per_page / page). Une requete a la
        fois (rate limit ~2 req/s sur customer_invoices). Si un lot renvoie 500,
        decoupe-le en deux lots plus petits et reessaie.
      - Si 500 aussi : passe a (c).
   c. run_operations en lecture, UNE operation a la fois, avec path_params=
      {"company_id": <id>} et query_params={"page": 1, "per_page": 100}, en testant
      les operation_id candidats dans cet ordre : "list_customer_invoices",
      "get_customer_invoices", "customer_invoices", "getCompanyCustomerInvoices",
      "invoices". Si la reponse est "Opération inconnue", essaie le candidat suivant.
      NE PAS appeler search_operations (500).
   d. Dans tous les cas, filtre TOI-MEME client-side : ne retiens que les factures non
      soldees (payment_status different de "paid", ou paid != true, ou
      remaining_amount_with_tax > 0) dont la deadline (payment_deadline / deadline /
      due_date) est anterieure a aujourd'hui. Champs usuels d'une facture : id, number,
      date, payment_deadline, currency, amount_with_tax, remaining_amount_with_tax,
      paid, status, customer_id.
3. PAIEMENTS ET CLIENTS — outils curates qui FONCTIONNENT pour cette entreprise :
   - list_customer_payments(company_id=<id>, page=1, ...) en paginant (champ
     pagination) : chaque paiement a id, date, customer_id, customer_name, invoice_id,
     amount, currency, method, status. Somme les paiements par invoice_id pour
     verifier le reste du de chaque facture.
   - list_customers(company_id=<id>, page=1, per_page=100, ...) en paginant (champ
     pagination.total) ou get_customer(company_id=<id>, customer_id=<id>) pour les
     emails (champ emails) et les conditions de paiement de chaque client.
4. Construis une liste JSON d'objets {invoice_id, number, customer_id, customer_name,
   customer_email, date, deadline, currency, amount, amount_paid, status} — mapping :
   invoice_id=id, number=number, date=date, deadline=payment_deadline,
   amount=amount_with_tax, amount_paid=amount_with_tax - remaining_amount_with_tax
   (ou somme des paiements list_customer_payments), status=status — puis appelle
   compute_relance_report(invoices_json=<la liste JSON>, today=<YYYY-MM-DD du jour>).
   Ne calcule JAMAIS les jours de retard ou les etapes toi-meme : laisse l'outil le faire.
5. Ta reponse finale est UNIQUEMENT le rapport markdown retourne par l'outil (champ
   report_markdown), suivi d'une phrase indiquant qu'aucun envoi n'a ete effectue (POC).

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
    tools=[compute_relance_report],
    connectors=[agents.connector("pennylane_e376")],
    builtins=agents.Builtins(skills=False, subagents=False),
    guardrails=[ReadOnlyPennylaneGuardrail()],
    max_iterations=60,
)
