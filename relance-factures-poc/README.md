# relance-factures-poc

POC **lecture seule** de l'agent « Relance de facture » : un agent Mistral (Agents SDK) qui
lit les factures clients impayées dans **Pennylane** via le connecteur `pennylane_e376`
(connexion de l'utilisateur, on-behalf-of) et produit un **rapport à blanc** du plan de
relance — aucun envoi, aucune écriture (garde-fou `ReadOnlyPennylaneGuardrail`).

## Règles métier appliquées (outil déterministe `compute_relance_report`)

- J = date d'échéance (date de paiement demandé), pas la date d'émission.
- Seuils : **J+15** 1re relance (très cordial) · **J+30** 2e relance (courtois) ·
  **J+45** 3e relance (plus ferme) · **J+60** 4e relance (ferme mais respectueux, email +
  appel ciblé) · **J+90** mise en demeure (formel, courrier recommandé).
- Facture dont la **date d'émission = date d'échéance** → **pas de 1re relance**.
- Factures soldées (reste dû = 0) ou non encore échues → exclues.

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
