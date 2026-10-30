# Source languages and file-format support

MaintainerLens retrieves evidence from allowlisted public-repository files.
"Supported" means eligible for indexing and retrieval, subject to file, syntax,
content and preparation limits. It does **not** mean compiling the repository,
running its tests, proving a root cause or verifying a fix. Model comprehension
depends on the configured model and context; equal report quality across these
languages has not been measured.

## Language-aware source chunking

| Language | Recognized extensions | Structural support |
| --- | --- | --- |
| Python | `.py`, `.pyi` | Python syntax analysis; function/class/method metadata |
| JavaScript | `.js`, `.jsx` | Tree-sitter declarations and method metadata |
| TypeScript | `.ts`, `.tsx` | Tree-sitter declarations and method metadata |
| Go | `.go` | Tree-sitter declaration metadata |
| Java | `.java` | Tree-sitter type/member metadata |

The adaptive chunker uses syntax boundaries where practical while retaining
line ranges and size/token limits. It is not whole-program static analysis:
there is no compiler, type checker or complete call graph. Parser errors can
fail preparation rather than promise support for every language version.
Unrecognized structures in otherwise parseable files can use text windows.

## Text-window source support

| Language | Recognized extensions | Current treatment |
| --- | --- | --- |
| Rust | `.rs` | Text windows; no registered syntax analyzer |
| C | `.c`, `.h` | Text windows; no registered syntax analyzer |
| C++ | `.cpp`, `.hpp` | Text windows; no registered syntax analyzer |
| C# | `.cs` | Text windows; no registered syntax analyzer |
| Ruby | `.rb` | Text windows; no registered syntax analyzer |
| Shell | `.sh` | Text windows; no registered syntax analyzer |
| HTML | `.html` | Text windows; no DOM analysis |
| CSS | `.css` | Text windows; no stylesheet parser |

Listed suffixes are the actual allowlist, not examples of all equivalent
suffixes. For example, `.cc` and `.cxx` are not currently recognized as C++.

## Documents and configuration

| Format | Recognized extensions or filenames | Treatment |
| --- | --- | --- |
| Markdown | `.md` | Heading-aware boundaries where practical |
| MDX, reStructuredText, plain text | `.mdx`, `.rst`, `.txt`, `LICENSE` | Text windows; MDX is not parsed as JSX |
| JSON, YAML, TOML, XML | `.json`, `.yaml`, `.yml`, `.toml`, `.xml` | Text windows |
| SQL, Terraform, Properties | `.sql`, `.tf`, `.properties` | Text windows; no execution or semantic validation |
| Build/container files | `Dockerfile`, `Dockerfile.*`, `Makefile` | Text windows |
| Ignore and environment examples | `.gitignore`, `.dockerignore`, `.env.example`, `.env.sample` | Text windows |

Filename/suffix matching is case-insensitive. Unknown types such as `.php`,
`.kt`, `.swift`, `.vue`, `.svelte` and `.ipynb` are skipped; a repository's presence
on GitHub does not imply all its source is covered. History Issues/PRs/comments
can be retrieved independently of source-file language support.

Real `.env` variants except the two examples, selected lockfiles and directories
such as `.git`, `.venv`, `node_modules`, `dist` and `build` are excluded. Only
regular supported files within existing content/size budgets are eligible.
These rules are not a general-purpose secret scanner.

## Implementation and quality boundary

The authoritative implementation is the
[file-type allowlist](../src/maintainer_lens/core/source_types.py),
[tree filter](../src/maintainer_lens/clients/github_tree.py),
[syntax registry](../src/maintainer_lens/services/syntax_registry.py) and
[adaptive chunker](../src/maintainer_lens/services/source_chunker.py).

Existing retrieval measurements use small Python-oriented samples. The initial
[report review cases](REPORT_QUALITY.md#existing-review-cases) cover a small set of
Python repositories; they do not validate report quality for other listed languages. Neither
language registration nor a successful pipeline proves semantic accuracy.
