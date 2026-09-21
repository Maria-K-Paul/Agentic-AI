# Student Records Agent — LangChain + Gemini

A LangChain agent that answers questions about students by **choosing its own tools**.
There is no fixed chain: the LLM looks at the question, picks a tool, looks at the
result, and decides whether it needs another one.

## Files

| File | What it does |
| --- | --- |
| `create_db.py` | Creates `students.db` (SQLite) and inserts the 5 student rows |
| `agent.py` | The 4 tools + the Gemini agent |
| `requirements.txt` | Dependencies |
| `.env.example` | Template for your API key — copy to `.env` |

## Setup

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Create the database**

```bash
python create_db.py
```

**3. Add your Gemini API key**

Get a free key at <https://aistudio.google.com/app/apikey>, then copy
`.env.example` to `.env` and paste the key in:

```
GOOGLE_API_KEY=AIza...your_key_here
```

## Run

```bash
python agent.py
```

That runs all four sample questions plus the challenge question. You can also ask
your own:

```bash
python agent.py "Who scored the most in AI, 22CS047 or 22CS049?"
```

Or go interactive:

```bash
python agent.py --chat
```

## The four tools

| Tool | Input | Returns |
| --- | --- | --- |
| `get_student_info` | `student_id: str` | Name, department |
| `get_student_marks` | `student_id: str` | Python, Database, AI, Web marks |
| `calculator` | `expression: str` | Result of the arithmetic |
| `get_passing_rules` | *(none)* | Min average 40%, min 35% per subject |

## How the agent picks tools

Three things do the work, and none of them is an `if` statement written by you:

1. **`@tool`** turns a plain function into a tool LangChain can hand to the LLM.
2. **The docstring becomes the tool description** — this is what the LLM actually
   reads when deciding. That is why each docstring says *when* to use the tool
   ("use this whenever the question involves marks...") and not just what it does.
3. **Type hints become the input schema** — `student_id: str` is what tells Gemini
   the tool needs a string called `student_id`, so it knows to extract `22CS045`
   from the question and pass it in.

`AgentExecutor` then runs the loop:

```
question -> LLM -> tool call -> result -> LLM -> another tool? -> ... -> final answer
```

Every run prints the tools the agent chose, e.g. for the challenge question:

```
TOOLS THE AGENT DECIDED TO USE:
    get_student_info()
      -> get_student_marks()
      -> get_passing_rules()
      -> calculator()
      -> calculator()
```

The exact order can vary between runs — that is the point. A fixed chain would
always do the same thing; an agent decides.

## Fixed chain vs. agentic workflow

| Fixed chain | Agent |
| --- | --- |
| You write the call order | The LLM picks the order |
| Same steps every time | Steps depend on the question |
| "What is the name?" still runs all 4 steps | Runs only `get_student_info()` |
| Breaks on an unanticipated question | Handles it if the tools are sufficient |

## Notes

- `calculator` only evaluates arithmetic. It parses the expression with `ast` and
  walks the tree, so it cannot run arbitrary code the way bare `eval()` would.
- `students.db` and `.env` are gitignored — the database is rebuildable with
  `create_db.py`, and the key should never be committed.
- To use a different model, set `GEMINI_MODEL` in `.env` (default `gemini-2.0-flash`).
