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
facture payée ; **facture avec paiement partiel (déjà payé > 0, reste dû > 0) jamais relancée**
(le solde est encaissé par prélèvement automatique — bucket `excluded_partial_payment`) ;
idempotence (jamais deux fois le même palier sur la même facture).

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

Valeurs concrètes **fournies et vérifiées le 2026-11-23** (constantes `PHASE2_CONFIG` dans
`main.py`) :

- **Compte e-mail** : `eric.doyen@id-interactive.fr` — vérifié : le connecteur `gmail` est
  authentifié sur ce compte (recherche `from:me` → expéditeur `eric.doyen@id-interactive.fr`).
- **Dossier Drive** : `Relance-factures`, id `1D3gWJnITWIpHHUKo13jgBClAmzWInDqP`, propriétaire
  `eric.doyen@id-interactive.fr`, `can_add_children=true`, vide à la création (vérifié via
  `google_drive_mcp_search_files`).
- **Modèles d'e-mail Pennylane de relance impayée** (kind `billing_email_invoice` attendu par
  `send_by_email`) : `Relance impayée j+15` (1re relance), `Relance impayée j+30` (2e),
  `Relance impayée j+45` (3e). Leurs **IDs numériques** sont résolus à l'exécution via la
  sonde lecture seule `fetch_pennylane_email_templates` (chemins sondés : `/email_templates`,
  `/billing_email_templates`, `/customer_invoice_templates`).

  **Résultat de la sonde exécutée en live le 2026-11-23** (déploiement `2e38202`, exécution
  `aed1ca07…285d430`, aucun envoi effectué) :
  - `GET /billing_email_templates` → **OK avec le token actuel** (aucun scope supplémentaire
    nécessaire pour lister) : 5 modèles, dont **`Relance impayée j+15` = id `153549164544`**
    (kind `billing_email_invoice`, locale `fr_FR`, sujet « Rappel – Facture N° {{numero}} en
    attente de règlement »), plus `new quote`, `new invoice`, `Envoi devis ID Interactive`,
    `Envoi facture ID Interactive`.
  - **`Relance impayée j+30` et `j+45` sont ABSENTS** (`relance_templates_found = 1` sur 3
    attendus) → à créer dans l'UI Pennylane avant la phase 2.
  - `GET /email_templates` → **404** (chemin inexistant).
  - `GET /customer_invoice_templates` → OK : 3 modèles de **PDF** de facture (« Modèle par
    défaut », ids 81833/70730/70729) — sans lien avec les modèles d'e-mail de relance.

Checklist restante :

1. ✅ **FAIT le 2026-11-27** — Nouvelle clé API Pennylane `relance-factures-poc` créée dans
   l'UI (Paramètres entreprise › Connectivité › Développeurs) avec le scope
   `customer_invoices:all` (+ `customers:readonly`, `ledger_accounts:readonly`) ; valeur
   enregistrée comme nouvelle version du secret workspace `cle_api_pennylane` dans
   Studio › Secrets Manager (binding du déploiement `relance-factures-poc` inchangé :
   `secret_name=cle_api_pennylane` → env `cle_api_pennylane`). Déploiement redéployé sur
   `5fe33a9` (v9) et **vérifié en live par le run dry-run du 2026-11-27** : `GET /me` vu
   par le worker retourne `customer_invoices:all` → `scopes_ok=true` dans `fetch_relance_plan`.
2. **Créer `Relance impayée j+30` et `Relance impayée j+45` dans l'UI Pennylane** — seul
   `Relance impayée j+15` existe (id `153549164544`, confirmé par la sonde en live le
   2026-11-23). La sonde les retrouvera automatiquement par leur nom
   (`PHASE2_CONFIG.relance_email_templates` dans `main.py`).
   ✅ **FAIT le 2026-11-28** : les 3 modèles existent dans l'UI (société 21994007, sous-onglet
   emails_and_annexes) avec les id `153549164544` (j+15, par défaut), `161268740096` (j+30),
   `161268875264` (j+45). Comme la liste API les exclut depuis oct. 2026 (voir addendum 11),
   le correctif **v10** les passe en **id explicites** (`PHASE2_CONFIG.relance_email_template_ids`,
   surchargeables par l'env `RELANCE_TEMPLATE_IDS="1:<id>,2:<id>,3:<id>"`) ; fallback par nom via
   la liste ; envoi réel refusé sans modèle résolu sauf `RELANCE_ALLOW_DEFAULT_TEMPLATE=true`.
3. **Connecteurs `gmail` et `google_drive_mcp`** déjà actifs et authentifiés sur le compte
   `eric.doyen@id-interactive.fr` ✅ ; pour les schedules (sans utilisateur), des
   **credentials workspace** seront peut-être nécessaires sur ces connecteurs.
4. Dossier Drive `Relance-factures` ✅ — y créer les sous-dossiers `rapports/`,
   `Mises en demeure/` et le journal `journal-relances.json` (créés automatiquement par
   l'agent en phase 2 s'ils n'existent pas).

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

## 9. Addendum 2026-11-25 — v9 implémentée sur la branche `vibe/relance-factures-poc-226b64`

L'architecture de la section 4 est codée dans `main.py` (l'agent garde le nom/déploiement
`relance-factures-poc`) :

- **3 outils serveur `client.*`** : `fetch_relance_plan` (plan du jour filtré par le journal,
  scope vérifié via `GET /me`), `send_pennylane_invoice_email` (validations serveur complètes :
  anti-doublon journal, palier recalculé, facture non soldée / sans paiement partiel, scope
  `customer_invoices:all`, plafond quotidien, modèle résolu par nom ; POST `send_by_email`,
  409 = retry ; retourne l'entrée de journal + journal mis à jour), `fetch_invoice_pdf_base64`
  (PDF via `public_file_url`, base64 ≤ 3 Mo sinon `drive_link_hint`).
- **Garde-fou v2 `RelanceGuardrail`** : `client.*` + lectures Pennylane + `gmail_create_draft`
  (+ lectures Gmail) + lectures/create/update/copy Drive ; bloque `run_operations`, tout envoi
  Gmail, les suppressions/permissions, `run_typescript` et les autres connecteurs.
- **Sécurités par défaut** : `RELANCE_DRY_RUN=true` (aucun envoi réel tant que non désactivé),
  `RELANCE_DAILY_SEND_CAP=20`. Journal Drive `journal-relances.json` (clé `invoice_id` + palier ;
  les entrées `dry_run: true` ne bloquent pas les envois réels).
- **Connecteurs ajoutés à l'agent** : `pennylane_e376`, `gmail`, `google_drive_mcp`.
- **Tests** : `uv run python test_agent.py` → `ALL LOCAL CHECKS PASSED` (règles métier,
  outils v9 mockés : dry-run sans POST, POST avec bon `email_template_id`, anti-doublon,
  palier, solde/paiement partiel, scope, 409, plafond, PDF trop gros ; garde-fou v2 allow/deny ;
  run scripté). `uv run agents info` → 9 outils `client.*`, 3 connecteurs, `RelanceGuardrail`.

**Reste avant le premier envoi réel** (section 3) : ✅ scope `customer_invoices:all` +
secret `cle_api_pennylane` faits et vérifiés en live (addendum 10) ; il reste les modèles
j+30/j+45 dans l'UI Pennylane, puis déploiement progressif selon la section 6 (étape 0 en
dry-run 1 semaine → paliers un par un).

## 10. Addendum 2026-11-27 — clé API `relance-factures-poc` (scope `customer_invoices:all`) en place, v9 redéployée et vérifiée en dry-run

- **Clé API** : nouveau token Pennylane `relance-factures-poc` (Paramètres entreprise ›
  Connectivité › Développeurs) avec `customer_invoices:all` + `customers:readonly` +
  `ledger_accounts:readonly` ; valeur stockée comme nouvelle version du secret workspace
  `cle_api_pennylane` (Studio › Secrets Manager). La valeur n'a jamais transité par le chat
  ni le repo.
- **Déploiement** : `relance-factures-poc` redéployé sur `5fe33a92183038474ebb7c8be05dc38856633483`
  (branche `vibe/relance-factures-poc-226b64`) ; binding secret conservé
  (`cle_api_pennylane` → env `cle_api_pennylane`, lu au boot du worker). Rollout `succeeded`,
  `deployed_revision = 5fe33a9…`, workers actifs.
- **Vérification live (dry-run, aucun envoi)** : exécution `aed1ca07…61c` du 2026-11-27,
  `company_id=21994007`. `fetch_relance_plan` → `scopes_ok=true` (`customer_invoices:all`
  présent dans `GET /me`), 3 161 factures lues (32 pages, GET uniquement), 62 factures en
  retard avec action prévue (reste dû 71 648,52 EUR) : J+15 = 5, J+30 = 11, J+45 = 4,
  J+60 = 12, J+90 = 30. 20 simulations `send_pennylane_invoice_email(dry_run=true)` :
  5 OK au palier 1 (modèle j+15 id `153549164544`), 15 bloquées côté modèle (j+30/j+45
  absents — item 2 de la checklist), **0 envoi réel**, 0 brouillon Gmail, 0 lettre.
- **Persistance Drive** : `journal-relances.json` créé (id `1yg8KewMYWBbLQnmnABvHkSTlZ-a5z2nV`,
  5 entrées `dry_run: true`), sous-dossier `rapports` créé (id `1FzlXMioz8qygCzCF1PgcM8TlHDl0ESgB`),
  rapport `rapport-2026-11-27.md` (id `1pjSwb1GI4ul6FqBsaXfZO5mxxAi-wNs0`).
- **Reste** : item 2 (modèles j+30/j+45 dans l'UI Pennylane) ; puis étape 0 du plan §6
  (dry-run 1 semaine) avant tout envoi réel.

## 11. Addendum 2026-11-27 — Changement API Pennylane (oct. 2026) : les modèles de RELANCE ne sont plus listés par `GET /billing_email_templates`

Vérification live le 2026-11-27 (exécution `aed1ca07…d68`, company_id 21994007, nouvelle clé
`relance-factures-poc`) : `GET /api/external/v2/billing_email_templates` ne retourne plus que
4 modèles (3045580 « new invoice », 3045576 « Envoi facture ID Interactive », 3045581
« new quote », 3045577 « Envoi devis ID Interactive »). **Aucun** des noms
« Relance impayée j+15 / j+30 / j+45 » n'y figure, et l'ancien modèle j+15 (id `153549164544`,
présent le 2026-11-23) en a disparu. Les 3 dry-runs ciblés (paliers 1/2/3) retournent
`template_id=null` + warning « absent de GET /billing_email_templates ».

**Cause (doc officielle `getBillingEmailTemplates`, mise à jour 2026-10-21)** : « Payment
reminders, GoCardless subscription emails, and PDF footer templates are **not included** »
dans `/billing_email_templates`. Depuis cette évolution, les modèles d'e-mails de relance
(paiement) sont exclus de la liste, même s'ils restent utilisables.

**Conséquence pour v9** : la résolution par nom via la liste ne fonctionne plus pour les
modèles de relance. `POST /customer_invoices/{id}/send_by_email` accepte toujours
`email_template_id` (kind `billing_email_invoice`, dont font partie les modèles de relance
créés dans l'UI), donc les envois restent possibles avec les bons IDs.

**Correctif prévu (v10)** : résolution des template_id par **configuration explicite**
(ex. env `RELANCE_TEMPLATE_IDS="1:<id j+15>,2:<id j+30>,3:<id j+45>"` ou mapping dans
`PHASE2_CONFIG`), la résolution par nom via la liste restant un fallback ; à défaut, omettre
`email_template_id` (Pennylane utilise alors le modèle d'envoi par défaut selon la langue du
client) avec warning. Les IDs numériques se lisent dans l'URL d'édition du modèle dans l'UI
Pennylane (Paramètres entreprise › Facturation clients › Emails).

## 12. Addendum 2026-11-28 — correctif v10 implémenté : template_id explicites pour les relances

Le changement API d'oct. 2026 (addendum 11) rendait la résolution par nom inopérante pour les
modèles de relance. Correctif codé dans `main.py` (branche `vibe/relance-factures-poc-226b64`) :

- `PHASE2_CONFIG["relance_email_template_ids"]` = `{1: 153549164544, 2: 161268740096, 3: 161268875264}`
  (ids lus dans l'UI Pennylane, société 21994007), surchargeable par l'env
  `RELANCE_TEMPLATE_IDS` au format `"1:<id>,2:<id>,3:<id>"` ;
- `_resolve_template()` : id explicite d'abord (source `config`, aucun appel à la liste), sinon
  matching du nom dans `GET /billing_email_templates` (source `list`) ;
- `send_pennylane_invoice_email` : si aucun modèle résolu → dry-run = warning, envoi réel =
  ToolError (refus), sauf si `RELANCE_ALLOW_DEFAULT_TEMPLATE=true` (POST sans `email_template_id`
  = modèle par défaut Pennylane, avec warning) ;
- `fetch_pennylane_email_templates` rapporte aussi `configured_template_ids` et la note oct. 2026 ;
- tests : env prioritaire (sans appel liste), fallback par nom, absent (warning/ToolError),
  allow-default (POST sans template) → `ALL LOCAL CHECKS PASSED`.
