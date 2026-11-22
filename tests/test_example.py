"""Knowledge graph regressions on examples/contas-a-receber. Standard library only."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "powerbi-architect" / "scripts" / "pbi_graph.py"
EXAMPLE = ROOT / "examples" / "contas-a-receber"


def graph(cwd: Path, *args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8")


class TestExampleGraph(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.tmp.name) / "repo"
        shutil.copytree(EXAMPLE, cls.repo / "contas-a-receber")
        r = graph(cls.repo, "build", ".")
        assert r.returncode == 0, r.stderr
        cls.report = (cls.repo / "powerbi-graph" / "GRAPH_REPORT.md").read_text(encoding="utf-8")
        cls.data = json.loads((cls.repo / "powerbi-graph" / "graph.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_example_has_no_findings(self):
        findings = self.report.split("## Findings", 1)[1].split("##", 1)[0]
        self.assertIn("none", findings)

    def test_report_is_much_smaller_than_sources(self):
        line = next(ln for ln in self.report.splitlines() if ln.startswith("**Token budget:**"))
        numbers = [int(n.replace(",", "")) for n in re.findall(r"≈ ([\d,]+)", line)]
        source, report = numbers[0], numbers[1]
        self.assertGreater(source / report, 8, line)

    def test_readme_commands_work(self):
        """The commands shown in README.md must answer on the example."""
        impact = graph(self.repo, "impact", "Fato Titulos[Dias Atraso]").stdout
        for expected in ("Valor Vencido", "Dias Atraso Medio", "Visão Geral"):
            self.assertIn(expected, impact)
        self.assertIn("Fato Titulos", graph(self.repo, "explain", "Valor Vencido").stdout)
        self.assertIn("Valor Vencido", graph(self.repo, "path", "Regional", "Valor Vencido").stdout)
        self.assertIn("Visão Geral", graph(self.repo, "query", "o que alimenta a página Visão Geral?").stdout)

    def rels(self, rel):
        nodes = {n["id"] if isinstance(n, dict) else n: n for n in (
            self.data["nodes"] if isinstance(self.data["nodes"], list) else self.data["nodes"].values())}
        edges = self.data.get("edges") or self.data.get("links")
        return [(nodes.get(e["source"], {}).get("label", e["source"]), nodes.get(e["target"], {}).get("label", e["target"]))
                for e in edges if e.get("rel") == rel]

    def test_hierarchy_levels_count_as_usage(self):
        targets = {t for _, t in self.rels("hierarchy_level")}
        self.assertTrue({"Calendario[Ano]", "Calendario[Mês]"} <= targets or {"Ano", "Mês"} <= targets, targets)

    def test_measure_in_dynamic_title_counts_as_usage(self):
        targets = {t for _, t in self.rels("uses_measure")}
        self.assertTrue(any("Titulo Vencido" in t for t in targets), targets)


class TestCrossModelFindings(unittest.TestCase):
    def test_shared_source_and_divergent_measure(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            shutil.copytree(EXAMPLE, repo / "a")
            shutil.copytree(EXAMPLE, repo / "b")
            m = repo / "b" / "ContasReceber.SemanticModel" / "definition" / "tables" / "_Medidas.tmdl"
            m.write_text(m.read_text(encoding="utf-8").replace(
                "CALCULATE ( [Valor Aberto], 'Fato Titulos'[Dias Atraso] > 0 )",
                "CALCULATE ( [Valor Aberto], 'Fato Titulos'[Dias Atraso] > 30 )"), encoding="utf-8")
            result = graph(repo, "build", ".")
            self.assertEqual(result.returncode, 0, result.stderr)
            rep = (repo / "powerbi-graph" / "GRAPH_REPORT.md").read_text(encoding="utf-8")
            findings = rep.split("## Findings", 1)[1].split("##", 1)[0]
            for title, detail in (
                ("Source objects read by more than one model", "dw.fato_titulos_receber"),
                ("Same measure name, different DAX in different models", "valor vencido"),
            ):
                with self.subTest(title=title):
                    line = next((line for line in findings.splitlines() if title in line), "")
                    self.assertIn(detail, line.lower())
            models = rep.split("## Semantic models", 1)[1].split("##", 1)[0]
            for folder in ("a", "b"):
                with self.subTest(folder=folder):
                    self.assertIn(f"`{folder}/ContasReceber.SemanticModel`", models)



if __name__ == "__main__":
    unittest.main()
