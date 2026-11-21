"""Analyste de donnees conversationnel sur CSV depuis Google Drive.

Outils fixes uniquement (chargement, description, filtrage, agregats) :
aucune execution de code arbitraire n'est possible (NoSandbox +
programmatic_tool_calling desactive + outils en model_access="direct").
"""

import base64
import binascii
import csv
import io
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from mistralai import agents

MAX_CSV_CHARS = 8_000_000
MAX_ROWS = 200_000
MAX_CELL_CHARS = 200
MAX_SAMPLE_ROWS = 3
MAX_FILTER_RETURN = 100
MAX_GROUPS_RETURN = 200
MAX_DESCRIBE_COLUMNS = 100
SAMPLE_FOR_INFERENCE = 2_000

_DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$")
_DATE_FR_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_BOOL_VALUES = {"true", "false", "oui", "non", "yes", "no"}


def _is_date(s: str) -> bool:
    return bool(_DATE_ISO_RE.match(s) or _DATE_FR_RE.match(s))

_DRIVE_READ_TOOLS = {
    "search_files",
    "list_recent_files",
    "get_file_metadata",
    "get_file_permissions",
    "download_file_content",
    "read_file_content",
}


class Dataset:
    def __init__(self, source: str, columns: list[str], rows: list[dict[str, str]], types: dict[str, str], delimiter: str) -> None:
        self.source = source
        self.columns = columns
        self.rows = rows
        self.types = types
        self.decimal_comma = delimiter != ","


_DATASETS: dict[str, Dataset] = {}


def _get_dataset(ctx: agents.ToolCallbackContext) -> Dataset:
    ds = _DATASETS.get(ctx.session.id)
    if ds is None:
        raise agents.ToolError(
            "Aucun CSV n'est charge en memoire pour cette session. "
            "Recharge le fichier avec load_csv (passe le contenu texte ou le champ content base64 de download_file_content)."
        )
    return ds


def _to_float(s: str, decimal_comma: bool) -> float | None:
    s = s.strip().replace("\u00a0", "").replace(" ", "")
    if not s:
        return None
    if decimal_comma:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _infer_type(values: list[str], decimal_comma: bool) -> str:
    n = 0
    number = date = boolean = 0
    for v in values:
        s = v.strip()
        if s == "":
            continue
        n += 1
        if _to_float(s, decimal_comma) is not None:
            number += 1
        elif _is_date(s):
            date += 1
        elif s.lower() in _BOOL_VALUES:
            boolean += 1
    if n == 0:
        return "empty"
    if number / n >= 0.9:
        return "number"
    if date / n >= 0.9:
        return "date"
    if boolean / n >= 0.9:
        return "bool"
    return "text"


def _truncate_cell(value: str, limit: int = MAX_CELL_CHARS) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _truncate_row(row: dict[str, str], limit: int = MAX_CELL_CHARS) -> dict[str, str]:
    return {k: _truncate_cell(v, limit) for k, v in row.items()}


class Aggregation(BaseModel):
    column: str = Field(description="Nom exact de la colonne a agreger (obligatoire sauf pour count).")
    function: Literal["count", "count_distinct", "sum", "avg", "min", "max"] = Field(
        description="count = nombre de lignes du groupe ; count_distinct = valeurs distinctes non vides de la colonne ; sum/avg/min/max = statistique numerique de la colonne."
    )


@agents.tool(model_access="direct", annotations=agents.ToolAnnotations(read_only_hint=True))
def load_csv(
    ctx: agents.ToolCallbackContext,
    /,
    source_name: Annotated[str, Field(description="Nom du fichier source, ex. 'ventes_2025.csv'.")],
    csv_text: Annotated[str | None, Field(description="Contenu CSV en texte clair. Utilise ce champ OU base64_content.")] = None,
    base64_content: Annotated[str | None, Field(description="Contenu CSV encode en base64, tel que renvoye par le champ 'content' de download_file_content (connecteur Google Drive). Utilise ce champ OU csv_text.")] = None,
) -> dict[str, Any]:
    """Charge un fichier CSV dans la memoire de la session (un seul jeu de donnees actif : un nouveau chargement remplace l'ancien).

    Retourne le nombre de lignes, les colonnes et un echantillon. Les questions suivantes utilisent describe_columns, filter_rows et group_aggregate sur ce jeu charge.
    """
    if csv_text is None and base64_content is None:
        raise agents.ToolError("Fournis csv_text ou base64_content.")
    raw = csv_text
    if raw is None:
        try:
            raw = base64.b64decode(base64_content, validate=True).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            raise agents.ToolError("base64_content invalide : passe exactement le champ 'content' renvoye par download_file_content.")
    raw = raw.lstrip("\ufeff")
    if len(raw) > MAX_CSV_CHARS:
        raise agents.ToolError(f"Fichier trop grand : {len(raw)} caracteres, maximum {MAX_CSV_CHARS}. Demande a l'utilisateur un extrait.")
    try:
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(raw), dialect)
    raw_rows = list(reader)
    if not raw_rows:
        raise agents.ToolError("Le CSV est vide.")
    header = [h.strip() for h in raw_rows[0]]
    seen: dict[str, int] = {}
    for i, h in enumerate(header):
        if h == "":
            h = f"colonne_{i + 1}"
        if h in seen:
            seen[h] += 1
            h = f"{h} ({seen[h]})"
        else:
            seen[h] = 1
        header[i] = h
    rows: list[dict[str, str]] = []
    for r in raw_rows[1:]:
        if not r or all(c.strip() == "" for c in r):
            continue
        row = {col: (r[i].strip() if i < len(r) else "") for i, col in enumerate(header)}
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise agents.ToolError(f"Trop de lignes : plus de {MAX_ROWS}. Demande a l'utilisateur un extrait.")
    if not rows:
        raise agents.ToolError("Le CSV ne contient aucune ligne de donnees (une seule ligne d'en-tete).")
    decimal_comma = dialect.delimiter != ","
    types = {col: _infer_type([row[col] for row in rows[:SAMPLE_FOR_INFERENCE]], decimal_comma) for col in header}
    ds = Dataset(source_name, header, rows, types, dialect.delimiter)
    previous = _DATASETS.get(ctx.session.id)
    _DATASETS[ctx.session.id] = ds
    return {
        "status": "loaded",
        "source": source_name,
        "replaced_previous": previous.source if previous else None,
        "rows": len(rows),
        "columns": header,
        "column_types": types,
        "sample_rows": [_truncate_row(r, 80) for r in rows[:MAX_SAMPLE_ROWS]],
    }


@agents.tool(model_access="direct", annotations=agents.ToolAnnotations(read_only_hint=True))
def dataset_info(ctx: agents.ToolCallbackContext, /) -> dict[str, Any]:
    """Rappelle quel fichier CSV est actuellement charge dans cette session : nom source, nombre de lignes, colonnes et types inferes. A utiliser pour te souvenir du fichier entre les questions."""
    ds = _get_dataset(ctx)
    return {"source": ds.source, "rows": len(ds.rows), "columns": ds.columns, "column_types": ds.types}


@agents.tool(model_access="direct", annotations=agents.ToolAnnotations(read_only_hint=True))
def describe_columns(
    ctx: agents.ToolCallbackContext,
    /,
    columns: Annotated[list[str] | None, Field(description="Colonnes a decrire (noms exacts). Par defaut : toutes les colonnes.")] = None,
) -> dict[str, Any]:
    """Decrit les colonnes du CSV charge : type infere, valeurs nulles, nombre de valeurs distinctes, min/max/moyenne pour les nombres, exemples et valeurs les plus frequentes pour le texte."""
    ds = _get_dataset(ctx)
    selected = columns if columns else ds.columns
    unknown = [c for c in selected if c not in ds.columns]
    if unknown:
        raise agents.ToolError(f"Colonnes inconnues : {unknown}. Colonnes disponibles : {ds.columns}")
    truncated = len(selected) > MAX_DESCRIBE_COLUMNS
    if truncated:
        selected = selected[:MAX_DESCRIBE_COLUMNS]
    described: list[dict[str, Any]] = []
    for col in selected:
        values = [row[col] for row in ds.rows]
        non_null = [v for v in values if v != ""]
        distinct = sorted({v for v in non_null})
        info: dict[str, Any] = {
            "column": col,
            "type": ds.types[col],
            "non_null": len(non_null),
            "null": len(values) - len(non_null),
            "distinct": len(distinct),
            "examples": [_truncate_cell(v, 80) for v in non_null[:3]],
        }
        if ds.types[col] == "number":
            nums = [n for n in (_to_float(v, ds.decimal_comma) for v in non_null) if n is not None]
            if nums:
                info["min"] = min(nums)
                info["max"] = max(nums)
                info["mean"] = round(sum(nums) / len(nums), 6)
        elif ds.types[col] == "date" and non_null:
            info["min"] = min(non_null)
            info["max"] = max(non_null)
        if ds.types[col] == "text" and len(distinct) <= 20:
            counts: dict[str, int] = {}
            for v in non_null:
                counts[v] = counts.get(v, 0) + 1
            info["top_values"] = [
                {"value": _truncate_cell(v, 80), "count": c} for v, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
            ]
        described.append(info)
    return {"source": ds.source, "rows": len(ds.rows), "described": described, "columns_truncated": truncated}


@agents.tool(model_access="direct", annotations=agents.ToolAnnotations(read_only_hint=True))
def filter_rows(
    ctx: agents.ToolCallbackContext,
    /,
    column: Annotated[str, Field(description="Nom exact de la colonne a filtrer.")],
    operator: Annotated[
        Literal["eq", "ne", "gt", "ge", "lt", "le", "contains", "startswith", "in", "is_null", "is_not_null"],
        Field(description="eq/ne = egal/different ; gt/ge/lt/le = superieur/... (nombres ou texte) ; contains/startswith = sous-chaine/prefixe (texte, insensible a la casse) ; in = valeur dans une liste ; is_null/is_not_null = cellule vide ou non."),
    ],
    value: Annotated[str | list[str] | None, Field(description="Valeur de comparaison. Pour 'in', passe une liste de valeurs. Inutilise pour is_null/is_not_null.")] = None,
    limit: Annotated[int, Field(description="Nombre maximal de lignes retournees (1-100).")] = 20,
) -> dict[str, Any]:
    """Filtre les lignes du CSV charge selon une condition sur une colonne. Retourne le nombre de lignes correspondantes et un echantillon des lignes (tronquees)."""
    ds = _get_dataset(ctx)
    if column not in ds.columns:
        raise agents.ToolError(f"Colonne inconnue : '{column}'. Colonnes disponibles : {ds.columns}")
    limit = max(1, min(limit, MAX_FILTER_RETURN))
    is_number = ds.types[column] == "number"
    numeric_compare = operator in {"gt", "ge", "lt", "le"} and is_number
    target_num: float | None = None
    target_str: str | None = None
    targets_num: list[float] = []
    targets_str: list[str] = []
    if operator in {"gt", "ge", "lt", "le", "eq", "ne"}:
        if value is None or isinstance(value, list):
            raise agents.ToolError(f"L'operateur '{operator}' exige une valeur scalaire (chaine ou nombre).")
        if is_number and _to_float(value, ds.decimal_comma) is not None:
            target_num = _to_float(value, ds.decimal_comma)
        else:
            target_str = value.strip().casefold()
    elif operator == "in":
        if not isinstance(value, list) or not value:
            raise agents.ToolError("L'operateur 'in' exige une liste non vide de valeurs.")
        for v in value:
            if is_number and _to_float(v, ds.decimal_comma) is not None:
                targets_num.append(_to_float(v, ds.decimal_comma))  # type: ignore[arg-type]
            else:
                targets_str.append(v.strip().casefold())
    elif operator in {"contains", "startswith"}:
        if value is None or isinstance(value, list):
            raise agents.ToolError(f"L'operateur '{operator}' exige une valeur scalaire.")
        target_str = value.strip().casefold()

    def matches(cell: str) -> bool:
        if operator == "is_null":
            return cell == ""
        if operator == "is_not_null":
            return cell != ""
        if is_number and operator in {"eq", "ne"} and target_num is not None:
            n = _to_float(cell, ds.decimal_comma)
            return n is not None and (n == target_num if operator == "eq" else n != target_num)
        if numeric_compare:
            n = _to_float(cell, ds.decimal_comma)
            if n is None or target_num is None:
                return False
            if operator == "gt":
                return n > target_num
            if operator == "ge":
                return n >= target_num
            if operator == "lt":
                return n < target_num
            return n <= target_num
        s = cell.strip().casefold()
        if operator == "eq":
            return s == target_str
        if operator == "ne":
            return s != target_str
        if operator == "contains":
            return target_str is not None and target_str in s
        if operator == "startswith":
            return target_str is not None and s.startswith(target_str)
        if operator == "in":
            if is_number:
                n = _to_float(cell, ds.decimal_comma)
                return n is not None and (n in targets_num or cell.strip().casefold() in targets_str)
            return s in targets_str
        if operator in {"gt", "ge", "lt", "le"}:
            t = target_str or ""
            if operator == "gt":
                return s > t
            if operator == "ge":
                return s >= t
            if operator == "lt":
                return s < t
            return s <= t
        return False

    matched = [row for row in ds.rows if matches(row[column])]
    shown = matched[:limit]
    return {
        "source": ds.source,
        "column": column,
        "operator": operator,
        "value": value,
        "matched": len(matched),
        "returned": len(shown),
        "truncated": len(matched) > len(shown),
        "rows": [_truncate_row(r) for r in shown],
    }


@agents.tool(model_access="direct", annotations=agents.ToolAnnotations(read_only_hint=True))
def group_aggregate(
    ctx: agents.ToolCallbackContext,
    /,
    group_by: Annotated[list[str], Field(description="1 a 3 colonnes de groupement (noms exacts), ex. ['region'] ou ['region', 'mois'].")],
    aggregations: Annotated[list[Aggregation], Field(description="Agregats a calculer par groupe, ex. [{'column': 'montant', 'function': 'sum'}, {'column': 'client', 'function': 'count_distinct'}].")],
    limit: Annotated[int, Field(description="Nombre maximal de groupes retournes (1-200).")] = 200,
) -> dict[str, Any]:
    """Calcule des agregats par groupe sur le CSV charge : pour chaque combinaison de valeurs des colonnes 'group_by', applique chaque agregation (count, count_distinct, sum, avg, min, max)."""
    ds = _get_dataset(ctx)
    if not group_by or len(group_by) > 3:
        raise agents.ToolError("group_by doit contenir 1 a 3 colonnes.")
    unknown = [c for c in group_by if c not in ds.columns]
    if unknown:
        raise agents.ToolError(f"Colonnes de groupement inconnues : {unknown}. Colonnes disponibles : {ds.columns}")
    if not aggregations:
        raise agents.ToolError("aggregations ne peut pas etre vide.")
    aggregations = [a if isinstance(a, Aggregation) else Aggregation.model_validate(a) for a in aggregations]
    for agg in aggregations:
        if agg.function != "count" and agg.column not in ds.columns:
            raise agents.ToolError(f"Colonne d'agregation inconnue : '{agg.column}'. Colonnes disponibles : {ds.columns}")
        if agg.function in {"sum", "avg", "min", "max"} and ds.types.get(agg.column) != "number":
            raise agents.ToolError(f"'{agg.function}' exige une colonne numerique, or '{agg.column}' est de type '{ds.types.get(agg.column)}'.")
    limit = max(1, min(limit, MAX_GROUPS_RETURN))

    numeric_cols = sorted({agg.column for agg in aggregations if agg.function in {"sum", "avg", "min", "max"}})
    distinct_cols = sorted({agg.column for agg in aggregations if agg.function == "count_distinct"})
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in ds.rows:
        key = tuple(row[c] for c in group_by)
        g = groups.get(key)
        if g is None:
            g = {"count": 0, "_sum": {}, "_n": {}, "_distinct": {}, "_min": {}, "_max": {}}
            groups[key] = g
        g["count"] += 1
        for col in numeric_cols:
            n = _to_float(row[col], ds.decimal_comma)
            if n is None:
                continue
            g["_sum"][col] = g["_sum"].get(col, 0.0) + n
            g["_n"][col] = g["_n"].get(col, 0) + 1
            g["_min"][col] = n if col not in g["_min"] else min(g["_min"][col], n)
            g["_max"][col] = n if col not in g["_max"] else max(g["_max"][col], n)
        for col in distinct_cols:
            if row[col] != "":
                g["_distinct"].setdefault(col, set()).add(row[col])

    results: list[dict[str, Any]] = []
    for key, g in groups.items():
        out: dict[str, Any] = {col: key[i] if key[i] != "" else None for i, col in enumerate(group_by)}
        out["count"] = g["count"]
        for agg in aggregations:
            name = f"{agg.function}({agg.column})"
            if agg.function == "count":
                out[name] = g["count"]
            elif agg.function == "count_distinct":
                out[name] = len(g["_distinct"].get(agg.column, ()))
            elif agg.function == "sum":
                out[name] = round(g["_sum"][agg.column], 6) if agg.column in g["_sum"] else None
            elif agg.function == "avg":
                n = g["_n"].get(agg.column, 0)
                out[name] = round(g["_sum"][agg.column] / n, 6) if n else None
            elif agg.function == "min":
                out[name] = g["_min"].get(agg.column)
            elif agg.function == "max":
                out[name] = g["_max"].get(agg.column)
        results.append(out)
    truncated = len(results) > limit
    return {
        "source": ds.source,
        "rows": len(ds.rows),
        "group_by": group_by,
        "groups": len(results),
        "returned": min(len(results), limit),
        "truncated": truncated,
        "results": results[:limit],
    }


class ReadOnlyGuardrail(agents.Hook):
    """N'autorise que les outils d'analyse CSV (client.*) et Google Drive en lecture seule.

    Bloque notamment run_typescript / search_tool_functions / tout outil builtin :
    aucune execution de code arbitraire n'est possible.
    """

    async def pre_tool_call(self, ctx, call):
        if call.tool_id.startswith("client."):
            return None
        if call.tool_name in _DRIVE_READ_TOOLS:
            return None
        return agents.skip(
            f"L'outil '{call.tool_name}' n'est pas autorise et ne sera pas execute : cet agent n'utilise que ses outils "
            "d'analyse CSV (load_csv, dataset_info, describe_columns, filter_rows, group_aggregate) et le connecteur "
            "Google Drive en lecture seule. Aucun code arbitraire ne peut etre execute."
        )


INSTRUCTIONS = """Tu es un analyste de donnees conversationnel, et tu reponds en francais. L'utilisateur te donne un lien vers un fichier CSV dans son Google Drive, puis te pose des questions en langage naturel sur ces donnees. Tu dois repondre avec des chiffres reels, calcules sur ses donnees, jamais inventes.

CAPACITES FIXES (tu n'as aucun outil d'execution de code, et tu ne dois jamais en reclamer un) :
- Connecteur Google Drive en LECTURE SEULE : search_files, list_recent_files, get_file_metadata, get_file_permissions, download_file_content, read_file_content. N'utilise jamais create_file, copy_file ou update_file.
- load_csv : charge un CSV dans la memoire de la session (remplace le precedent).
- dataset_info : rappelle le fichier actuellement charge (source, lignes, colonnes, types).
- describe_columns : decrit les colonnes (type, nulls, distincts, min/max/moyenne, exemples).
- filter_rows : filtre les lignes selon une condition sur une colonne.
- group_aggregate : calcule des agregats (count, count_distinct, sum, avg, min, max) par groupe de colonnes.

PROTOCOLE :
1. Quand l'utilisateur te donne un lien Drive (https://drive.google.com/file/d/<ID>/view?..., ou open?id=<ID>, ou un dossier), extrais l'identifiant du fichier. Appelle get_file_metadata(file_id=<ID>) pour verifier le titre, puis download_file_content(file_id=<ID>) pour recuperer le contenu. Passe alors le champ 'content' renvoye (base64) a load_csv(base64_content=..., source_name=<titre du fichier>). Si le lien ne contient pas d'ID, retrouve le fichier avec search_files (ex. query "title contains '<morceau du nom>' and mimeType contains 'csv'") puis get_file_metadata/download_file_content sur l'ID trouve ; si plusieurs fichiers correspondent, choisis le plus recent (modified_time) et dis lequel tu as choisi.
2. Ensuite, reponds aux questions UNIQUEMENT a l'aide de describe_columns, filter_rows et group_aggregate sur le CSV charge. N'invente jamais un chiffre : si tu n'as pas calcule une valeur avec un outil, ne la donne pas. Pour une question du type "combien de X", "moyenne de Y par Z", "les lignes ou ...", traduis-la en appels d'outils filtres/agregats, puis formule la reponse.
3. Reponds en francais, de maniere concise et structuree, avec des tableaux Markdown quand c'est pertinent. Mentionne les colonnes/filtres utilises et le nombre de lignes concernees.

MEMOIRE DE CONVERSATION : la session conserve tout l'historique. "Le fichier", "ces donnees", "la colonne X", "comme avant" designent le dernier CSV charge (verifie avec dataset_info si besoin). Ne demande jamais a l'utilisateur de se repeter. Les questions de suivi portent sur le meme fichier charge. Si l'utilisateur fournit un nouveau lien, charge ce nouveau fichier (il remplace l'ancien) et dis-le clairement. Si aucun fichier n'est charge et qu'on te parle de "ces donnees", demande le lien Drive.

ERREURS : si un outil renvoie une erreur (colonne inconnue, type incompatible, fichier trop grand), lis le message, corrige l'appel ou explique clairement le probleme a l'utilisateur. Ne devine pas.
"""

agent = agents.Agent(
    model="mistral-large-4",
    name="csv-data-analyst",
    description="Analyste de donnees conversationnel : lit un CSV depuis Google Drive (connexion de l'utilisateur) et repond en langage naturel avec des chiffres reels issus des donnees. Outils fixes, sans execution de code.",
    instructions=INSTRUCTIONS,
    tools=[load_csv, dataset_info, describe_columns, filter_rows, group_aggregate],
    connectors=[agents.connector("google_drive_mcp")],
    # NoSandbox (default environment): no computer is provisioned, so no code can
    # execute. programmatic_tool_calling stays on only because connectors require it
    # in the config; the ReadOnlyGuardrail below skips run_typescript and every
    # non-allowlisted tool before it can run.
    builtins=agents.Builtins(skills=False, subagents=False),
    guardrails=[ReadOnlyGuardrail()],
    max_iterations=40,
)
