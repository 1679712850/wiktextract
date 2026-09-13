"""Cache-only adapter for wikitextprocessor's Wiki services.

The dependency currently has no offline option. Its service functions are
wrapped here, with normal behavior preserved for ordinary Wtp instances.
Importing this module when unpickling OfflineWtp also installs the adapters
in spawned extraction and thesaurus workers.
"""

import json
from functools import wraps

from wikitextprocessor import Wtp, interwiki, luaexec, wikidata


class OfflineWtp(Wtp):
    """Expand local pages and templates without fetching Wiki data."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Single-page and database-only runs also need the local table.
        interwiki.init_interwiki_map(self)


def _cache_only(module, name):
    """Dispatch only offline contexts to the cache-only implementation."""
    original = getattr(module, name)

    def decorate(offline_function):
        @wraps(original)
        def wrapped(wtp, *args, **kwargs):
            if isinstance(wtp, OfflineWtp):
                return offline_function(wtp, *args, **kwargs)
            return original(wtp, *args, **kwargs)

        setattr(module, name, wrapped)
        return wrapped

    return decorate


@_cache_only(interwiki, "get_interwiki_data")
def _interwiki_data(wtp):
    return []


@_cache_only(wikidata, "query_wikidata")
def _query(wtp, query):
    return {}


@_cache_only(wikidata, "statement_query")
def _statement(wtp, prop, item_id, lang_code):
    cached = wikidata.get_statement_cache(wtp, prop, item_id)
    if cached is None:
        return ""
    return wikidata.format_statement_result(cached[0], cached[1], prop)


@_cache_only(wikidata, "query_item")
def _item(wtp, item_id, lang_code):
    return wikidata.get_item_cache(wtp, item_id) or wikidata.WikiDataItem(
        item_id=item_id
    )


@_cache_only(wikidata, "query_entity_id_for_title")
def _entity_id(wtp, title, site_id):
    cached = wikidata.get_entity_id_cache(wtp, title, site_id)
    return None if cached == "not found" else cached


@_cache_only(wikidata, "get_entity_data")
def _entity_data(wtp, item_id):
    if item_id is None:
        item_id = _entity_id(wtp, wtp.title or "", "")
    if item_id is None:
        return None
    cached = wikidata.get_item_cache(wtp, item_id)
    if cached is None or not cached.entity_data:
        return None
    return json.loads(cached.entity_data)


@_cache_only(luaexec, "lua_loader")
def _lua_loader(wtp, modname):
    source = _lua_loader.__wrapped__(wtp, modname)
    if modname != "mw_wikibase" or source is None:
        return source
    # The upstream Lua bridge constructs an entity even when Python returns
    # None. Missing offline data must instead produce Lua nil, so templates
    # can use their usual fallback without a Lua execution error.
    return (
        "local wikibase = (function()\n" + source + "\nend)()\n"
        """
        function wikibase.getEntity(id)
            local data = mw_wikibase_getEntity_py(id)
            if data == nil then return nil end
            return require("mw.wikibase.entity").create(data)
        end
        wikibase.getEntityObject = wikibase.getEntity
        return wikibase
        """
    )
