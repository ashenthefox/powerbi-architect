# Histórico de mudanças

Todas as mudanças relevantes do projeto ficam registradas aqui. O formato segue o
[Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e as versões seguem o
[Versionamento Semântico](https://semver.org/lang/pt-BR/).

## [Não publicado]
### Corrigido
- O grafo distingue modelos semânticos de mesmo nome em pastas diferentes ao identificar
  fontes compartilhadas e medidas com DAX divergente, e mostra os caminhos no relatório.

## [1.2.0] — 2026-11-21
### Adicionado
- Projeto PBIP de exemplo em `examples/contas-a-receber`, validado pelos schemas da Microsoft, sem
  nenhum achado na auditoria nem no grafo.
- Avaliações publicadas: com a skill, 16 de 18 itens e 8 de 10 casos; sem a skill, 6 de 18 e 2 de 10
  (`evals/results/2026-11-21`). Materiais de apoio dos casos em `evals/fixtures/`.
- Auditoria: códigos de regra (`PBI001`…`PBI043`) com arquivo e linha, `--help`, `--version`,
  `--list-rules`, `--json`, `--sarif`, `--fail-on`, supressão por `.pbiaudit.json` e por anotação TMDL,
  correspondência com as regras do Best Practice Analyzer do Tabular Editor (inclusive a anotação
  `BestPracticeAnalyzer_IgnoreRules`) e regras novas para que cada um dos oito princípios tenha
  verificação automática. Catálogo em `docs/regras-auditoria.md`.
- Modelos de página PBIR (`templates/pbir-pages`) e tema acessível (`templates/theme`).
- Referências `semantic-features.md` (calculation groups, field parameters, visual calculations, funções
  DAX) e `fabric-and-cicd.md` (Direct Lake, Git do Fabric, deployment pipelines, CI).
- Plugin do Claude Code: `/plugin install powerbi-architect --marketplace DaviMark/powerbi-architect`.
- Instaladores: `--status` e `--update` (`-Status` e `-Update` no PowerShell).
- Releases com o zip da skill e atestado de procedência (`gh attestation verify`).
- `ROADMAP.md`.
- Arquivos de comunidade: formulários de issue (comportamento da IA, bug, ideia, resultado de avaliação,
  dúvida), template de pull request, `CODEOWNERS`, `SUPPORT.md` e labels gerenciadas
  (`.github/labels.json`).
- Workflow `release`: uma tag `vX.Y.Z` confere a versão do `SKILL.md` e o `CHANGELOG.md`, roda os testes
  e publica uma release no GitHub com a skill em `.zip`.
- `.gitattributes` mantém o `install.sh` com finais de linha LF em clones no Windows; `.editorconfig`.
### Alterado
- CI roda os testes no Linux e no Windows, do Python 3.9 ao 3.13, valida os JSON pelos schemas da
  Microsoft e confere todo exemplo de código das referências.
- README: números de tokens medidos no projeto de exemplo (reproduzíveis), no lugar dos números de um
  projeto que não estava no repositório.
- `CONTRIBUTING.md` ampliado (fluxo de trabalho, versões, processo de release e regra de idiomas).
- Documentação para pessoas (`SECURITY.md`, `evals/README.md`, `CHANGELOG.md`) e nomes dos jobs de CI
  em português do Brasil. Arquivos lidos pela IA continuam em inglês.
- O Dependabot agrupa as atualizações de GitHub Actions em um único PR mensal.
- CI: `actions/checkout` 7.0.1, `actions/setup-python` 7.1.0, `actions/setup-node` 7.1.0.
- Os instaladores e o `pbi_graph.py` mostram `[ok]` no lugar de um símbolo de check, para que a saída
  apareça igual em qualquer terminal e página de código. O banner do README desenha os checks como
  vetores.
### Corrigido
- `audit_pbip.py --help` era tratado como caminho de pasta e gerava um BLOCKER.
- A auditoria podia falhar no console do Windows ao imprimir símbolos como → e ≤.
- O grafo apontava como sem uso as medidas usadas em título dinâmico de visual e as colunas usadas só em
  hierarquias.
- O `install.sh` tratava opções desconhecidas como pasta de destino.

## [1.1.0] — 2026-11-21
### Adicionado
- `scripts/pbi_graph.py`: grafo de conhecimento offline do Power BI (TMDL, PBIR, SQL, documentação,
  Python) com `GRAPH_REPORT.md`, cards por modelo, `graph.json`, `graph.html` e os comandos `query` /
  `explain` / `impact` / `path`. Achados: colunas e medidas sem uso, campos quebrados em visuais, fontes
  compartilhadas e medidas divergentes.
- `pbi_graph.py install|uninstall`: instruções para agentes no `AGENTS.md`/`CLAUDE.md`, hooks do Claude
  Code (início de sessão e após edições) e hooks do git que mantêm o grafo atualizado.
- `references/graph.md`; a seção 0 do `SKILL.md` agora diz "grafo antes dos arquivos".
- Os instaladores geram o grafo depois de copiar (`--no-graph`/`-NoGraph`, `--no-hooks`/`-NoHooks`).
### Alterado
- Framework renomeado: PBIP Blueprint → Power BI Workspaces (`config/workspaces.toml`, `tools/pbw.py`).
### Corrigido
- O `install.ps1` não era interpretado pelo Windows PowerShell 5.1 (UTF-8 sem BOM); agora é salvo com BOM.

## [1.0.0] — 2026-11-23
### Adicionado
- `SKILL.md` com oito princípios, workflow e árvores de decisão.
- Referências: workflow, modeling, power-query, dax, tmdl, pbir, report-design, visual-selection,
  custom-visuals, documentation, refactoring, performance, security, quality-checklist.
- Visual customizado de referência verificado em `templates/pbiviz-bullet-kpi` (API 5.11, pbiviz 7.2).
- Auditoria offline de PBIP em `scripts/audit_pbip.py`.
- Instaladores por projeto e globais (bash e PowerShell), avaliações e CI.
