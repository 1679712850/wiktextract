import bz2
import io
import json
import multiprocessing
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from wikitextprocessor import Wtp, interwiki, wikidata

from wiktextract.offline import OfflineWtp
from wiktextract.wiktwords import main


def check_worker(wtp):
    """Exercise an unpickled context in a fresh interpreter."""
    import sqlite3

    with patch(
        "requests.sessions.Session.request",
        side_effect=AssertionError("unexpected network access"),
    ):
        wtp.db_conn = sqlite3.connect(wtp.db_path)
        try:
            interwiki.init_interwiki_map(wtp)
            return (
                wikidata.query_item_label(wtp, "Q1"),
                wikidata.get_entity_data(wtp, "Q1"),
            )
        finally:
            wtp.close_db_conn()


class TestOffline(TestCase):
    def setUp(self):
        network_patch = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("unexpected network access"),
        )
        self.network = network_patch.start()
        self.addCleanup(network_patch.stop)
        self.wtp = OfflineWtp(lang_code="zh")
        self.addCleanup(self.wtp.close_db_conn)
        self.wtp.start_page("測試")

    def test_missing_data_does_not_fetch_or_poison_cache(self):
        before = list(self.wtp.db_conn.iterdump())
        self.assertEqual(interwiki.get_interwiki_map(self.wtp), {})
        self.assertEqual(wikidata.query_wikidata(self.wtp, "SELECT"), {})
        self.assertEqual(wikidata.query_item_label(self.wtp, "Q1"), "")
        self.assertEqual(wikidata.query_item_desc(self.wtp, "Q1"), "")
        self.assertEqual(
            wikidata.statement_query(self.wtp, "P1", "Q1", "zh"), ""
        )
        self.assertIsNone(
            wikidata.query_entity_id_for_title(self.wtp, "測試", "")
        )
        self.assertIsNone(wikidata.get_entity_data(self.wtp, "Q1"))
        self.assertIsNone(wikidata.get_entity_data(self.wtp, None))
        self.assertIsNone(
            wikidata.mw_wikibase_getSitelink(self.wtp, "Q1", None)
        )
        self.assertEqual(before, list(self.wtp.db_conn.iterdump()))
        self.network.assert_not_called()

    def test_cached_data_and_local_template_expansion(self):
        entity = {
            "id": "Q1",
            "schemaVersion": 2,
            "sitelinks": {"zhwiktionary": {"title": "測試"}},
        }
        wikidata.insert_item(
            self.wtp,
            wikidata.WikiDataItem("Q1", "標籤", "描述", json.dumps(entity)),
        )
        wikidata.save_statement_cache(
            self.wtp, "Q1", "標籤", "描述", "P1", "property", "value", None
        )
        wikidata.save_entity_id_cache(
            self.wtp, "測試", "", "Q1", "標籤", "描述"
        )
        self.wtp.db_conn.execute(
            "INSERT INTO interwiki_maps VALUES (?, ?, ?, ?)",
            ("w", "https://zh.wikipedia.org/wiki/$1", 0, 0),
        )
        interwiki.init_interwiki_map(self.wtp)
        self.assertIn("w", interwiki.get_interwiki_map(self.wtp))
        self.assertEqual(wikidata.query_item_label(self.wtp, "Q1"), "標籤")
        self.assertEqual(wikidata.query_item_desc(self.wtp, "Q1"), "描述")
        self.assertEqual(wikidata.get_entity_data(self.wtp, None), entity)
        self.assertEqual(
            wikidata.mw_wikibase_getSitelink(self.wtp, "Q1", None), "測試"
        )
        self.wtp.add_page("Template:local", 10, "local {{{1}}}")
        self.assertEqual(self.wtp.expand("{{local|data}}"), "local data")
        self.assertEqual(
            self.wtp.expand("{{#statements:P1|from=Q1}}"), "value"
        )
        self.wtp.add_page(
            "Module:cached",
            828,
            """return {test = function()
                return mw.wikibase.getEntity('Q1'):getId() .. '|' ..
                    mw.wikibase.getEntityObject('Q1'):getId()
            end}""",
            model="Scribunto",
        )
        self.assertEqual(
            self.wtp.expand("{{#invoke:cached|test}}"), "Q1|Q1"
        )
        self.network.assert_not_called()

    def test_lua_wiki_services_without_cached_data(self):
        self.wtp.add_page(
            "Module:offline",
            828,
            """return {test = function()
                return table.concat({
                    tostring(mw.wikibase.getEntity('Q1')),
                    tostring(mw.wikibase.getEntityIdForCurrentPage()),
                    tostring(mw.wikibase.getEntityIdForTitle('測試')),
                    tostring(mw.wikibase.getSitelink('Q1')),
                    mw.wikibase.getLabel('Q1'),
                    mw.wikibase.getDescription('Q1'),
                    tostring(next(mw.site.interwikiMap()))
                }, '|')
            end}""",
            model="Scribunto",
        )
        self.assertEqual(
            self.wtp.expand("{{#invoke:offline|test}}"),
            "nil|nil|nil|nil|||nil",
        )
        self.network.assert_not_called()

    def test_spawned_worker_stays_offline(self):
        self.wtp.db_conn.close()
        self.wtp.db_conn = None
        try:
            with ProcessPoolExecutor(
                max_workers=1, mp_context=multiprocessing.get_context("spawn")
            ) as executor:
                self.assertEqual(
                    executor.submit(check_worker, self.wtp).result(timeout=30),
                    ("", None),
                )
        finally:
            import sqlite3

            self.wtp.db_conn = sqlite3.connect(self.wtp.db_path)

    def test_normal_context_still_uses_network(self):
        online = Wtp(lang_code="zh")
        self.addCleanup(online.close_db_conn)
        self.network.side_effect = None
        self.network.return_value = Mock(
            ok=True, json=lambda: {"query": {"interwikimap": [{"prefix": "w"}]}}
        )
        self.assertEqual(
            interwiki.get_interwiki_data(online), [{"prefix": "w"}]
        )
        self.network.assert_called_once()

    def test_cli_offline_local_page(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            page = folder / "page.txt"
            output = folder / "output.jsonl"
            page.write_text(
                "TITLE: 測試\n==漢語==\n===名詞===\n# 本地提取。\n",
                encoding="utf-8",
            )
            with patch.object(
                sys,
                "argv",
                [
                    "wiktwords",
                    "--offline",
                    "--edition", "zh",
                    "--db-path", str(folder / "pages.db"),
                    "--page", str(page),
                    "--out", str(output),
                ],
            ):
                main()
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["word"], "測試")
            self.assertEqual(data["senses"][0]["glosses"], ["本地提取。"])
            self.network.assert_not_called()

    def test_cli_dump_and_database_reprocessing(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            dump = folder / "dump.xml.bz2"
            database = folder / "pages.db"
            output = folder / "output.jsonl"
            with bz2.open(dump, "wt", encoding="utf-8") as stream:
                stream.write(
                    "<mediawiki><page><title>測試</title><ns>0</ns>"
                    "<revision><model>wikitext</model><text>"
                    "==漢語==\n===名詞===\n# 本地提取。\n"
                    "</text></revision></page></mediawiki>"
                )
            base_args = [
                "wiktwords", "--offline", "--edition", "zh",
                "--db-path", str(database), "--out", str(output),
                "--num-processes", "1",
            ]
            with patch.object(
                sys, "argv", base_args + [str(dump), "--skip-extraction"]
            ):
                main()
            self.assertEqual(output.read_text(), "")
            with patch.object(sys, "argv", base_args):
                main()
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["word"], "測試")
            self.assertEqual(data["senses"][0]["glosses"], ["本地提取。"])
            self.network.assert_not_called()

    def test_missing_brown_corpus_fails_without_download(self):
        with (
            patch.object(
                sys, "argv", ["wiktwords", "--offline", "--edition", "en"]
            ),
            patch("nltk.corpus.brown") as brown,
            patch("nltk.download") as download,
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            brown.ensure_loaded.side_effect = LookupError("missing corpus")
            with self.assertRaises(SystemExit) as raised:
                main()
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("local NLTK Brown corpus", stderr.getvalue())
            download.assert_not_called()
