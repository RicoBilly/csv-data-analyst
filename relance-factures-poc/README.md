# relance-factures-poc

Agent **« Relance de facture »** (Mistral Agents SDK) : il lit les factures clients impayées
dans **Pennylane** (API Company v2 via outils serveur `client.*`, token lu dans l'environnement
du worker depuis le secret workspace ; connecteur MCP `pennylane_e376` en secours) et produit
le plan de relance. La phase 2 (v9) ajoute les actions réelles **en dry-run par défaut**
(`RELANCE_DRY_RUN=true`) : envois e-mail Pennylane J+15/30/45, brouillon Gmail avec PDF pour
J+60, lettre de mise en demeure stockée dans Drive pour J+90, journal Drive anti-doublon.

## Règles métier appliquées (outil déterministe `compute_relance_report`)

- J = date d'échéance (date de paiement demandé), pas la date d'émission.
- Seuils : **J+15** 1re relance (très cordial) · **J+30** 2e relance (courtois) ·
  **J+45** 3e relance (plus ferme) · **J+60** 4e relance (ferme mais respectueux, email +
  appel ciblé) · **J+90** mise en demeure (formel, courrier recommandé).
- Facture dont la **date d'émission = date d'échéance** → **pas de 1re relance**.
- Factures soldées (reste dû = 0) ou non encore échues → exclues.
- Facture ayant reçu un **paiement partiel** (déjà payé > 0, reste dû > 0) → **jamais relancée** :
  le solde est encaissé par **prélèvement automatique** (listée à part dans
  `excluded_partial_payment`).

## Run locally

```bash
uv run python test_agent.py   # local checks, no API key needed
```

## Deploy (Mistral-managed, monorepo subdirectory)

Build context = repo root, Dockerfile = `relance-factures-poc/Dockerfile`:

```
deployment_name: relance-factures-poc
hardened: true   (connecteur Pennylane en on-behalf-of)
```

The deployed agent registers as a workflow named `relance-factures-poc`; run it from this
session with `execute_workflow` (input = session start message), and later attach a daily
schedule with `create_workflow_schedule` (phase 2, nécessite des credentials workspace sur
les connecteurs car les schedules n'ont pas d'utilisateur).

## Phase 2 (v9) — implémentée, dry-run par défaut

Nouveaux outils serveur (`client.*`, le token et les gros payloads ne transitent jamais par
le contexte de l'agent) :

- `fetch_relance_plan(company_id, today, journal_json)` : plan du jour calculé côté serveur
  (GET `/me`, `/customers`, `/customer_invoices`), filtré par le journal Drive
  (`journal-relances.json`), chaque facture annotée d'une action : `pennylane_email` (niveaux
  1-3), `gmail_draft` (J+60), `drive_letter` (J+90).
- `send_pennylane_invoice_email(company_id, invoice_id, stage, dry_run, journal_json)` :
  validations serveur (facture non soldée, pas de paiement partiel, palier recalculé == demandé,
  anti-doublon journal, scope `customer_invoices:all` via `GET /me`, plafond quotidien
  `RELANCE_DAILY_SEND_CAP` = 20, modèle d'email résolu par nom dans `/billing_email_templates`),
  puis `POST /customer_invoices/{id}/send_by_email` (204 = en file d'envoi, 409 = PDF pas encore
  généré → retry). Retourne l'entrée de journal + le journal mis à jour à réécrire dans Drive.
- `fetch_invoice_pdf_base64(company_id, invoice_id)` : PDF via `public_file_url`, base64 si
  ≤ 3 Mo (limite PJ brouillon Gmail), sinon `drive_link_hint`.

Garde-fou v2 `RelanceGuardrail` : autorise `client.*`, les lectures Pennylane
(`list_*`/`get_*`/`query_*` + curatés + `search_operations`), `gmail_create_draft` (+ lectures
Gmail), les lectures/créations/mises à jour Drive ; **bloque** `run_operations`, tout envoi
Gmail (`gmail_send_email`, `gmail_send_draft`), les suppressions/labels Gmail, les suppressions
et permissions Drive, `run_typescript` et tout autre connecteur.

Sécurités par défaut : `RELANCE_DRY_RUN=true` (aucun POST tant que non désactivé explicitement),
plafond `RELANCE_DAILY_SEND_CAP=20` envois/jour. Le journal Drive (`journal-relances.json` dans
le dossier `Relance-factures`) est la source de vérité anti-doublon — clé `(invoice_id, stage)`
(les entrées `dry_run: true` ne bloquent pas un envoi réel ultérieur).

Prérequis avant tout envoi réel (voir [PHASE2-FAISABILITE.md](PHASE2-FAISABILITE.md) §3) :
scope `customer_invoices:all` sur la clé API + mise à jour du secret `cle_api_pennylane`,
création des modèles « Relance impayée j+30 » / « j+45 » dans l'UI Pennylane (seul j+15 existe,
id `153549164544`), et éventuellement des credentials workspace sur `gmail`/`google_drive_mcp`
pour un schedule sans utilisateur.
