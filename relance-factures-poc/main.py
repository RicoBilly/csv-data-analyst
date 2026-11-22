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

READ_ONLY_TOOLS = {
    "get_context",
    "get_company",
    "get_customer",
    "list_accounts",
    "get_changelog",
    "query_trial_balance",
    "get_vat_returns",
    "search_operations",
    "run_operations",
}

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
    """N'autorise que les outils Pennylane en lecture seule et les outils client.*.

    Bloque run_operations pour toute operation_id ressemblant a une ecriture, tout outil
    Pennylane hors liste, run_typescript et tout autre outil : le POC est 100% lecture
    seule, aucun envoi ni aucune modification n'est possible.
    """

    async def pre_tool_call(self, ctx, call):
        if call.tool_id.startswith("client."):
            return None
        if call.tool_id.startswith(PENNYLANE_PREFIX):
            name = call.tool_name
            if name in READ_ONLY_TOOLS:
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
                f"Outils autorises : {', '.join(sorted(READ_ONLY_TOOLS))}."
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

CONNECTEUR PENNYLANE (pennylane_e376) - lecture seule :
- Outils autorises : get_context, get_company, get_customer, list_accounts, get_changelog,
  query_trial_balance, get_vat_returns, search_operations, run_operations.
- run_operations n'est autorise que pour des operations en LECTURE (GET) decouvertes via
  search_operations avec verb="GET" : ne l'utilise jamais pour envoyer, creer, modifier
  ou supprimer quoi que ce soit (le garde-fou bloque les operations d'ecriture).

PROTOCOLE :
1. Appelle get_context() sans company_id pour lister les entreprises, choisis
   l'entreprise de l'agence et retiens son company_id. Appelle ensuite get_company
   (company_id) pour son nom, et get_context(company_id=...) si besoin de contexte.
2. Liste les factures clients : utilise l'outil de liste dedie s'il existe, sinon
   search_operations(query="list customer invoices", verb="GET", company_id=<id>) puis
   run_operations avec l'operation_id trouve, en paginant (cursor / page) jusqu'au bout.
   L'API ne filtre pas par statut cote serveur : filtre TOI-MEME, client-side, les
   factures finalisees non soldees dont la deadline est anterieure a aujourd'hui.
   Respecte les rate limits (environ 2 requetes/s sur customer_invoices) : pagine
   proprement, sans rafale.
3. Pour chaque facture retenue : recupere les paiements (search_operations "payments
   customer invoice" verb="GET", puis run_operations sur /customer_invoices/{id}/payments)
   et/ou les matched_transactions pour calculer le reste du ; appelle
   get_customer(company_id=<id>, customer_id=<id>) pour l'email du client et ses
   conditions de paiement.
4. Construis une liste JSON d'objets {invoice_id, number, customer_id, customer_name,
   customer_email, date, deadline, currency, amount, amount_paid, status} et appelle
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
    max_iterations=40,
)
