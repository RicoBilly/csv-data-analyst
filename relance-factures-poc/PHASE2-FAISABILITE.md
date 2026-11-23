# Revue de faisabilité — Phase 2 : agent de relance avec envois réels

Date : 2026-11-23. Périmètre : société Pennylane **ID Interactive** (`company_id` 21994007,
SIRET 490491594, Arradon). S'appuie sur le POC lecture seule v8 (`main.py`, déploiement
`relance-factures-poc`, branche `vibe/relance-factures-poc-226b64`).

**Verdict : FAISABLE**, avec 4 prérequis côté utilisateur (section 3) et les réserves
détaillées en section 5. Toutes les capacités ci-dessous ont été vérifiées contre la
documentation Pennylane (readme.io, 2026-11) et les connecteurs réellement connectés au
workspace (section 2).

## 1. Ce que la phase 2 doit réaliser

Chaque jour (ex. 8h00, Europe/Paris) : lire les factures clients Pennylane, calculer le plan
de relance (moteur déterministe existant `compute_relance_report`), puis exécuter :

| Palier | Action | Canal technique |
|---|---|---|
| J+15 | 1re relance, très cordial | e-mail Pennylane (`send_by_email` + modèle J+15) |
| J+30 | 2e relance, courtois | e-mail Pennylane (`send_by_email` + modèle J+30) |
| J+45 | 3e relance, plus ferme | e-mail Pennylane (`send_by_email` + modèle J+45) |
| J+60 | 4e relance, ferme mais respectueux | **brouillon Gmail** avec PDF de la facture en pièce jointe + appel ciblé (humain) |
| J+90 | mise en demeure | **lettre rédigée par l'agent, stockée dans un fichier Drive** (envoi postal LRAR manuel) |

Règles métier déjà implémentées et à conserver : J = date d'échéance ; factures soldées ou
non échues exclues ; **date d'émission = échéance → pas de 1re relance** ; aucune relance sur
facture payée ; idempotence (jamais deux fois le même palier sur la même facture).

## 2. Capacités vérifiées (besoin → solution → preuve)

| Besoin | Solution | Preuve / contrat exact |
|---|---|---|
| Envoi e-mail Pennylane J+15/30/45 | `POST /api/external/v2/customer_invoices/{id}/send_by_email` (operationId `sendByEmailCustomerInvoice`), header `X-Company-Id`, Bearer = secret `cle_api_pennylane`. Corps optionnel `{recipients?: string[], email_template_id?: int}`. **204** = e-mail en file d'envoi, **409** = PDF pas encore généré (retry quelques minutes), 401/403/404. | Doc `pennylane.readme.io/reference/sendbyemailcustomerinvoice` : « requires scope `customer_invoices:all` ». Le MCP Pennylane (`pennylane_e376`) est **100 % lecture seule** (tous les outils curatés `read_only: true` ; seul `run_operations` est destructif → reste bloqué). |
| Tonalité graduée J+15/30/45 | 3 **modèles d'e-mail de facturation** Pennylane (kind `billing_email_invoice`), référencés par `email_template_id` | Le corps/objet viennent du modèle (défaut = modèle par défaut selon la langue du client). **Pas de création ni de liste de modèles via l'API** → les 3 modèles sont créés dans l'UI Pennylane, leurs IDs sont mis en config de l'agent. |
| Auto-vérification des scopes | `GET /api/external/v2/me` (aucun scope requis) renvoie user + société + scopes du token | Doc « Understand Scopes ». L'agent vérifie `customer_invoices:all` au démarrage et échoue avec un message clair sinon. |
| PDF de la facture pour le brouillon J+60 | champ `public_file_url` de la facture (lien signé, courte validité ~30 min) — à télécharger **au moment de l'envoi** depuis le worker (le sandbox atteint déjà `app.pennylane.com`) | Doc API v2 : pas d'endpoint de téléchargement dédié ; le PDF est exposé via `public_file_url`. |
| Brouillon Gmail + pièce jointe | Connecteur `gmail` (actif, authentifié) : `gmail_create_draft(to, subject, body, attachments=[{filename, mimeType, content(base64)}])`, **≤ 3 Mo au total**, contenu base64 ≤ 4 128 768 caractères | Schéma vérifié. Si PDF > ~3 Mo → lien Drive dans le corps à la place. `gmail_send_email` / `gmail_send_draft` existent mais **restent bloqués** : J+60 = brouillon uniquement, envoi manuel après revue. |
| Lettre de mise en demeure J+90 | Connecteur `google_drive_mcp` (actif, authentifié) : `google_drive_mcp_create_file(title, parent_id, text_content, content_mime_type="text/markdown", disable_conversion_to_google_type=true)` | Schéma vérifié. Infos légales de l'émetteur via `get_company` (ID Interactive, SIRET 490491594, Arradon). |
| État persistant / anti-doublon | Dossier Drive dédié (ex. `Relances ID Interactive/`) + `journal-relances.json` : lu via `search_files` + `read_file_content`, réécrit via `update_file` (remplace tout le contenu ; ne marche que sur les fichiers créés via `create_file` → OK, l'agent crée son journal). Clé d'idempotence = `(invoice_id, palier)` | Schémas `search_files` / `read_file_content` / `update_file` vérifiés. Le sandbox d'exécution n'est pas une source de vérité fiable entre runs → **le journal Drive est la source de vérité**. |
| Rapports quotidiens + statistiques | Fichiers Drive écrits chaque run : `rapports/rapport-YYYY-MM-DD.md` (plan + actions réalisées) et `stats-relances.csv` (par palier : nb, montant, date d'envoi ; recouvrement recalculé à chaque run via `remaining_amount_with_tax`) | `create_file` text/csv → converti en feuille Google par défaut (`disable_conversion_to_google_type` pour garder un CSV brut). |
| Planification quotidienne | `create_workflow_schedule` : `cron_expressions: ["0 8 * * 1-5"]`, `time_zone_name: "Europe/Paris"`, `policy: {overlap: 1 (skip), pause_on_failure: true}`, `input` = exécution standard | Contrat vérifié. Voir réserve R9 (credentials workspace nécessaires pour les schedules). |

## 3. Prérequis côté utilisateur (checklist avant v9)

1. **Scope `customer_invoices:all`** sur la clé API Pennylane (à côté de `customers:readonly`,
   `ledger_accounts:readonly`), puis mise à jour du secret workspace `cle_api_pennylane`
   (la valeur change → re-lier le secret au déploiement, comme déjà fait en console).
2. **3 modèles d'e-mail de facturation** créés dans l'UI Pennylane (kind `billing_email_invoice`) :
   « Relance 1 — J+15 (très cordial) », « Relance 2 — J+30 (courtois) », « Relance 3 — J+45
   (plus ferme) » ; leurs IDs seront en config de l'agent.
3. **Connecteurs `gmail` et `google_drive_mcp`** rattachés au bon compte Google (déjà actifs
   et authentifiés dans ce workspace) + droits sur le dossier Drive choisi ; pour les
   schedules (sans utilisateur), des **credentials workspace** seront peut-être nécessaires
   sur ces connecteurs.
4. **Dossier Drive dédié** (ex. `Relances ID Interactive/`) avec sous-dossiers `rapports/`,
   `Mises en demeure/`.

## 4. Architecture proposée (v9)

- **Nouveaux outils serveur `client.*`** (le token et les gros payloads ne transitent jamais
  par le contexte de l'agent ; limite sandbox 16 MiB) :
  - `fetch_relance_plan(company_id, today)` : agrège à la volée customers + factures v2 +
    calcule le plan via la logique existante, en excluant ce qui est déjà dans le journal ;
  - `send_pennylane_invoice_email(company_id, invoice_id, stage, dry_run)` : valide que la
    facture est bien dans le plan du jour et au bon palier, vérifie le journal, puis
    `POST send_by_email` avec `email_template_id` du palier (recipients laissés vides =
    e-mails du client Pennylane), retry sur 409, journalise le résultat, ne logue jamais
    le token ;
  - `fetch_invoice_pdf_base64(company_id, invoice_id)` : télécharge `public_file_url` et
    renvoie `{filename, mime_type, base64}` si < ~3 Mo, sinon `{drive_link_hint}`.
- **Actions agent via connecteurs** (guardrail v2) : `gmail_create_draft` (J+60, avec PJ) ;
  `google_drive_mcp_create_file` (lettre J+90, rapport, stats) ; lectures Drive du journal.
- **Guardrail v2 `RelanceGuardrail`** : autorise `client.*`, lectures Pennylane
  (`list_*`/`get_*`/`query_` + curatés), `gmail_create_draft` **uniquement** (bloque
  `gmail_send_email`, `gmail_send_draft`, suppressions), `google_drive_mcp`
  search/read/create/update/copy (bloque delete et gestion des permissions), **bloque
  `run_operations`** et tout autre connecteur / `run_typescript`.
- **Flux d'un run quotidien** : vérifier scopes (`/me`) → lire journal Drive → fetch plan →
  pour chaque action prévue : exécuter (ou dry-run) → mettre à jour le journal → écrire
  rapport + stats → rendre le résumé.

## 5. Risques et réserves

- **R1 — Tonalités** : l'API n'accepte pas de corps libre ; sans les 3 modèles, toutes les
  relances partiraient avec le modèle par défaut. Mitigation : prérequis n°2, sinon fallback
  documenté.
- **R2 — E-invoicing (réforme FR 2026-2027)** : la doc Pennylane avertit que **envoyer une
  facture éligible par e-mail empêche sa transmission ultérieure vers la PA**. Politique
  proposée : exclure du `send_by_email` les factures marquées éligibles e-invoice (champ à
  confirmer sur l'objet facture) et les relancer autrement ; à valider avec le support
  Pennylane.
- **R3 — 409 (PDF non généré)** : retry différé ; les factures < J+15 sont exclues de toute
  façon, donc le PDF est en pratique déjà prêt.
- **R4 — Double envoi** : mitigé par le journal Drive (clé `invoice_id` + palier) + schedule
  en `overlap: skip`. Pennylane n'expose pas d'historique d'envois lisible par API.
- **R5 — Destinataires** : laisser `recipients` vide (défaut = e-mails du client dans
  Pennylane) plutôt que de ressaisir des adresses.
- **R6 — Appel J+60 et LRAR J+90 restent manuels** : pas de connecteur téléphonie ni postal.
  L'agent fournit le brouillon Gmail prêt à envoyer, la fiche d'appel dans le rapport, et la
  lettre Drive prête à imprimer/envoyer. La lettre n'est **jamais envoyée automatiquement**.
- **R7 — PDF > 3 Mo** : lien Drive dans le corps du brouillon à la place de la pièce jointe.
- **R8 — Mémoire sandbox 16 MiB** : tout agrégat volumineux reste côté serveur (outils
  `client.*`) ; le PDF passe en un seul appel.
- **R9 — Schedules sans utilisateur** : `create_workflow_schedule` peut nécessiter des
  credentials **workspace** sur `gmail` / `google_drive_mcp` (le déploiement est déjà
  `hardened`). Fallback si refusé : exécution manuelle quotidienne via `execute_workflow`.
- **R10 — Responsabilité juridique** : la mise en demeure engage ID Interactive ; revue
  humaine systématique de la lettre Drive avant envoi postal.

## 6. Plan de déploiement progressif

1. **Étape 0** : prérequis (section 3) + v9 en `dry_run=true` : schedule quotidien 8h,
   plan + journal + rapports écrits, **aucun envoi** ; 1 semaine d'observation des rapports.
2. **Étape 1** : activer J+15 seul (`dry_run=false`, plafond ex. 20 envois/jour).
3. **Étape 2** : J+30, puis J+45 (après revue des rapports).
4. **Étape 3** : J+60 (brouillons Gmail avec PDF).
5. **Étape 4** : J+90 (lettres de mise en demeure dans Drive).

À chaque étape, le rapport quotidien Drive est revu avant d'activer le palier suivant.

## 7. Estimation d'effort (code v9)

Environ 1 journée de dev : 3 outils serveur (plan, envoi e-mail, PDF base64), guardrail v2,
journal/rapport/stats Drive, schedule, tests unitaires (mocks, comme `test_agent.py`) puis un
run dry-run de bout en bout sur le déploiement.

## 8. Questions ouvertes pour le décideur

1. Les 3 modèles d'e-mail Pennylane (J+15/30/45) seront-ils créés, et leurs IDs fournis ?
2. La clé API peut-elle passer en `customer_invoices:all` (secret `cle_api_pennylane` à
   mettre à jour) ?
3. Quel compte Gmail pour les brouillons J+60, et quel dossier Drive pour le journal et les
   lettres ?
4. Politique e-invoicing (R2) et plafond d'envois par jour en phase réelle ?
