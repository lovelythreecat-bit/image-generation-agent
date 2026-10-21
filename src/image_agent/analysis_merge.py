"""Merge repeated discovery bookkeeping without discarding compatible content.

Only the material-analysis DTO uses this policy. Request identities and audit
cardinality remain strict. Conflicting ownership, provenance and selection must
still be corrected by the model instead of being guessed locally.
"""

from copy import deepcopy

from pydantic import BaseModel


def _distinct(values):
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _mapping(value):
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def _restore_models(original, value):
    """Assignment validators must retain already-typed, unassigned fields."""
    if isinstance(original, BaseModel):
        return type(original).model_validate(value)
    if isinstance(original, dict) and isinstance(value, dict):
        return {key: _restore_models(original.get(key), item) for key, item in value.items()}
    if (
        isinstance(original, list)
        and original
        and isinstance(value, list)
        and all(isinstance(item, type(original[0])) for item in original)
        and isinstance(original[0], BaseModel)
    ):
        return [type(original[0]).model_validate(item) for item in value]
    return value


def _rows(rows, key, *, text=(), lists=(), minima=(), first=(), path):
    """Merge by ID in first-seen order; structural scalar conflicts stay errors."""
    if not isinstance(rows, list):
        return rows
    result, indexed = [], {}
    for raw in rows:
        row = _mapping(raw)
        if isinstance(row, dict) and path == "facts":
            # The DTO default has meaning; omission must not become permission
            # to overwrite an appearance observation with a marketing claim.
            row.setdefault("verifiability", "visible_appearance")
        identity = row.get(key) if isinstance(row, dict) else None
        if not isinstance(identity, str):
            result.append(row)  # Let the DTO report malformed entries normally.
            continue
        if identity not in indexed:
            indexed[identity] = row
            result.append(row)
            continue
        original = indexed[identity]
        for field, value in row.items():
            if field not in original:
                original[field] = value
            elif original[field] == value:
                continue
            elif field in text and isinstance(original[field], str) and isinstance(value, str):
                # A round-trip retains the combined description without repeating it.
                original[field] = "\n".join(
                    _distinct(original[field].split("\n") + value.split("\n"))
                )
            elif field in lists and isinstance(original[field], list) and isinstance(value, list):
                original[field] = _distinct(original[field] + value)
            elif (
                field in minima
                and type(original[field]) in (int, float)
                and type(value) in (int, float)
            ):
                original[field] = min(original[field], value)
            elif field in first:
                # Both source views remain in material_ids; one representative is sufficient.
                continue
            else:
                raise ValueError(f"conflicting duplicate {path}.{field}")
    return result


def _selection(value):
    value = _mapping(value)
    if not isinstance(value, dict):
        return value
    for key in (
        "subject_ids",
        "required_element_ids",
        "preferred_element_ids",
        "excluded_element_ids",
    ):
        if isinstance(value.get(key), list):
            value[key] = _distinct(value[key])
    required, preferred = (
        value.get("required_element_ids"),
        value.get("preferred_element_ids"),
    )
    if isinstance(required, list) and isinstance(preferred, list):
        value["preferred_element_ids"] = [item for item in preferred if item not in required]
    # Do not resolve selected/excluded overlap: it changes the user's intent.
    return value


def _issue_rows(rows, path):
    if not isinstance(rows, list):
        return rows
    result = _rows(
        rows,
        "issue_id",
        text=("message",),
        lists=("subject_ids", "material_ids", "element_ids", "fact_ids", "options"),
        path=path,
    )
    for row in result:
        if not isinstance(row, dict):
            continue
        for key in ("subject_ids", "material_ids", "element_ids", "fact_ids"):
            if isinstance(row.get(key), list):
                row[key] = _distinct(row[key])
        options = _rows(row.get("options", []), "id", text=("label",), path=path + ".options")
        row["options"] = options
        if isinstance(options, list):
            for option in options:
                if isinstance(option, dict) and option.get("selection") is not None:
                    option["selection"] = _selection(option["selection"])
    return result


def merge_analysis_duplicates(value):
    if not isinstance(value, dict):
        return value
    data = deepcopy(value)
    specs = (
        ("materials", "material_id", ("summary",), (), (), ()),
        (
            "subjects",
            "subject_id",
            (),
            ("material_ids", "identity_fact_ids"),
            (),
            ("representative_material_id",),
        ),
        ("facts", "fact_id", ("description",), ("evidence",), ("confidence",), ()),
        ("elements", "element_id", ("description",), ("fact_ids",), (), ()),
    )
    for name, key, text, lists, minima, first in specs:
        if name not in data:
            continue
        data[name] = _rows(
            data[name],
            key,
            text=text,
            lists=lists,
            minima=minima,
            first=first,
            path=name,
        )
        if isinstance(data[name], list):
            for row in data[name]:
                if isinstance(row, dict):
                    for field in lists:
                        if isinstance(row.get(field), list):
                            row[field] = _distinct(row[field])
    intent = _mapping(data.get("intent"))
    data["intent"] = intent
    if isinstance(intent, dict):
        if "proposal" in intent:
            intent["proposal"] = _selection(intent["proposal"])
        if isinstance(intent.get("focus_element_ids"), list):
            intent["focus_element_ids"] = _distinct(intent["focus_element_ids"])
        if "constraints" in intent:
            constraints = _rows(
                intent["constraints"],
                "constraint_id",
                text=("instruction",),
                lists=("subject_ids", "element_ids"),
                path="intent.constraints",
            )
            intent["constraints"] = constraints
            if isinstance(constraints, list):
                for row in constraints:
                    if isinstance(row, dict):
                        for key in ("subject_ids", "element_ids"):
                            if isinstance(row.get(key), list):
                                row[key] = _distinct(row[key])
    # An issue repeated in both buckets is one blocker with all messages/options.
    top = data.get("issues", [])
    unmet = intent.get("unmet_requirements", []) if isinstance(intent, dict) else []
    if isinstance(top, list) and isinstance(unmet, list):
        combined = _issue_rows(top + unmet, "issues")
        top_ids = {
            r.get("issue_id")
            for r in map(_mapping, top)
            if isinstance(r, dict) and isinstance(r.get("issue_id"), str)
        }

        def in_top(row):
            return (
                not isinstance(row, dict)
                or not isinstance(row.get("issue_id"), str)
                or row["issue_id"] in top_ids
            )

        data["issues"] = [r for r in combined if in_top(r)]
        if isinstance(intent, dict):
            intent["unmet_requirements"] = [r for r in combined if not in_top(r)]
    return _restore_models(value, data)
