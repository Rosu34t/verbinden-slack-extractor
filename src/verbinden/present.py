"""Pure, configuration-controlled member presentation."""
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from verbinden.models import LiveStatus, Profile


def select_fields(item: dict, field_map: Mapping[str, str]) -> dict:
    """Select configured fields and return independent values under new keys."""
    return {output_key: deepcopy(item[source_key])
            for source_key, output_key in field_map.items() if source_key in item}


def to_member_response(profile: Profile, status: LiveStatus | None,
                       field_map: Mapping[str, str]) -> dict:
    current = status if status is not None else LiveStatus(presence="away")
    internal: dict[str, Any] = {
        "id": profile.id, "name": profile.display_name, "iconUrl": profile.icon_url,
        "status": current.model_dump(mode="json", by_alias=True), "intro": profile.intro,
    }
    return select_fields(internal, field_map)
