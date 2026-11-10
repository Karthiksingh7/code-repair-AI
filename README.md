# Patchwork

Patchwork is a local, repository-aware automated program-repair workbench. Its
repair pipeline follows the progressive knowledge-injection method described
in the supplied research paper:

1. **Bug knowledge:** inspect the selected source function, reported error or
   stack trace, failing-test output, and user's bug description.
2. **Repository knowledge:** only if Bug Knowledge candidates fail the requested
   test command, expand context using available Python AST definitions and
   references, Git co-change history, related callers/callees, dependencies,
   and file history.
3. **Project knowledge:** only if the previous tier still has no passing patch,
   retrieve related project documentation and checked-in issue/history files.

At each tier the configured Groq model (`openai/gpt-oss-120b` by default)
generates up to three candidate diffs. Patchwork applies each candidate to its
own temporary copy and retains only candidates for which the selected test
command exits successfully. A passing patch is still a proposal: a person
reviews it before choosing **Apply patch**. Failed test output, generated code,
and source snippets are not retained.

## Requirements

- Python 3.10 or later
- Git
- A Groq API key
- A working test command for the repository

Test commands execute project code and can run arbitrary code. Only test
repositories you trust, and enable local test execution in the interface only
when you accept this risk. The application binds to `127.0.0.1`; do not expose
it to a network.

## Install and run on Windows

```powershell
cd "C:\Code Repair AI"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:GROQ_API_KEY = "your Groq API key"
python app.py
```

For subsequent starts, set your key in PowerShell and run `.\start_patchwork.bat`;
the launcher creates an isolated `.venv` and installs the declared dependencies
on its first run.

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). `GROQ_API_KEY` is read
from the application environment. It is not stored in the repository or
browser storage. The UI explicitly confirms before sending repository snippets,
bug reports, and retrieved context to the configured Groq service.

## Connect a repository

- **Local path:** enter the absolute path to a repository on the same computer
  running Patchwork.
- **Public GitHub repository:** enter its `https://github.com/owner/name` URL.
- **ZIP:** upload a local repository ZIP. ZIP extraction is restricted to the
  application-managed workspace, not arbitrary archive paths.

Repository scanning is local and does not require a Groq key. The interface
shows indexed files, project documentation, detected language, and an example
test command after a successful connection. A Groq key is required only when
you start AI repair generation. Enter the test command that reproduces the bug
and verify the exact file and line before starting a repair.

## Workflow and safety

- Python function analysis uses the standard-library `ast` module. This is a
  local Python analysis, not a complete cross-language call-graph builder.
- Repository file and history reads have size and timeout limits.
- Model prompts and returned diffs have size/file restrictions. Patches cannot
  change files outside indexed, existing application source/test files.
- GitHub sources are restricted to public HTTPS `github.com` clone URLs.
- Candidate validation runs in a temporary working copy, never in the user's
  working tree. Only explicitly reviewed, fully passing candidates may be
  applied to the original repository. Original content is checked again for
  changes before a patch is applied.
- There is no Docker sandbox in the current workflow. Therefore local test
  execution is opt-in and should only be enabled for trusted projects.
- Documentation and checked-in issue notes are retrieved with deterministic
  local text matching. The paper's SentenceTransformer/FAISS indexes, LangChain
  retrieval agent, manually curated angelic/runtime examples, GitHub Issues
  API, and large-scale Pass@k research evaluation are not implemented. Results
  from a single local repair are not the paper's benchmark results.

## Development checks

```powershell
python -m unittest discover -s tests -v
python -m py_compile app.py repair_engine.py
```
