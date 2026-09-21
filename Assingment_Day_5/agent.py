"""A LangChain agent (Gemini) that answers student questions using four tools.

The agent is NOT given a fixed sequence of calls. It is given four tools and
their descriptions, and the LLM decides which ones to call and in what order.

Usage:
    python create_db.py                 # once, to build students.db
    python agent.py                     # run all the demo questions
    python agent.py "your question"     # ask one question
    python agent.py --chat              # interactive mode
"""

import ast
import operator
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI

DB_PATH = Path(__file__).parent / "students.db"


def _fetch_student(student_id):
    """Fetch one row from students.db, or None if the id does not exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM students WHERE UPPER(student_id) = UPPER(?)",
        (student_id.strip(),),
    ).fetchone()
    conn.close()
    return row


# --------------------------------------------------------------------------
# Tool 1
# --------------------------------------------------------------------------
@tool
def get_student_info(student_id: str) -> str:
    """Get a student's name and department from their student ID (e.g. 22CS045).

    Use this whenever the question asks who a student is, what their name is,
    or which department/branch they are in. This tool does NOT return marks.
    """
    row = _fetch_student(student_id)
    if row is None:
        return f"No student found with ID {student_id}."
    return (
        f"Student ID: {row['student_id']}, "
        f"Name: {row['name']}, "
        f"Department: {row['department']}"
    )


# --------------------------------------------------------------------------
# Tool 2
# --------------------------------------------------------------------------
@tool
def get_student_marks(student_id: str) -> str:
    """Get a student's marks in all four subjects from their student ID (e.g. 22CS045).

    Returns the Python, Database, AI and Web marks out of 100. Use this whenever
    the question involves marks, scores, results, totals, averages, or whether a
    student passes. It returns the raw marks only -- it does not add them up.
    """
    row = _fetch_student(student_id)
    if row is None:
        return f"No student found with ID {student_id}."
    return (
        f"Marks for {row['student_id']} -> "
        f"Python: {row['python']}, Database: {row['database']}, "
        f"AI: {row['ai']}, Web: {row['web']}"
    )


# --------------------------------------------------------------------------
# Tool 3
# --------------------------------------------------------------------------
_ALLOWED_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node):
    """Evaluate an arithmetic-only AST. No names, calls or attributes allowed."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("only numbers and the operators + - * / // % ** are allowed")


@tool
def calculator(expression: str) -> str:
    """Evaluate a plain arithmetic expression and return the result.

    Use this for every calculation, such as adding marks to get a total
    (85 + 72 + 90 + 78) or dividing a total to get an average (325 / 4).
    Pass numbers only -- no variable names, no words, no units, no equals sign.
    """
    try:
        result = _safe_eval(ast.parse(expression, mode="eval"))
    except Exception as exc:
        return f"Could not evaluate {expression}: {exc}"
    if isinstance(result, float) and not result.is_integer():
        result = round(result, 2)
    return f"{expression} = {result}"


# --------------------------------------------------------------------------
# Tool 4
# --------------------------------------------------------------------------
@tool
def get_passing_rules() -> str:
    """Get the university's official rules for passing.

    Use this whenever the question is about passing, failing, eligibility, or
    whether a student meets the requirements. Takes no arguments.
    """
    return (
        "University passing rules:\n"
        "1. Minimum overall average: 40%\n"
        "2. Minimum mark in EACH subject: 35%\n"
        "A student passes only if BOTH conditions are satisfied."
    )


TOOLS = [get_student_info, get_student_marks, calculator, get_passing_rules]

SYSTEM_PROMPT = """You are a university student-records assistant.

You have tools that read the student database and the university rulebook.
Decide for yourself which tools you need, and call them one after another until
you can answer. Do not ask the user for information a tool can give you.

Rules:
- Never invent a name, department or mark. Always get them from a tool.
- Use the calculator tool for every arithmetic step (totals, averages).
  Do not do the arithmetic yourself.
- To decide pass/fail, get the marks, get the official rules, use the
  calculator, then check BOTH conditions: the overall average AND the minimum
  mark in every single subject.
- Answer in clear plain English, and show the numbers you used.
"""

PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_PROMPT),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ]
)


def build_agent(verbose=True):
    """Wire Gemini + the four tools into an agent that chooses its own tools."""
    load_dotenv()

    if not DB_PATH.exists():
        sys.exit("students.db not found. Run it first:  python create_db.py")
    if not os.getenv("GOOGLE_API_KEY"):
        sys.exit(
            "GOOGLE_API_KEY is not set.\n"
            "Copy .env.example to .env and paste your Gemini key into it.\n"
            "Get a free key at https://aistudio.google.com/app/apikey"
        )

    llm = ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        temperature=0,
    )
    agent = create_tool_calling_agent(llm, TOOLS, PROMPT)
    return AgentExecutor(
        agent=agent,
        tools=TOOLS,
        verbose=verbose,
        return_intermediate_steps=True,
        max_iterations=10,
    )


def ask(executor, question):
    """Ask one question and print the tools the agent chose plus the answer."""
    print("\n" + "=" * 78)
    print("QUESTION: " + question)
    print("=" * 78)

    result = executor.invoke({"input": question})

    chain = [step[0].tool for step in result.get("intermediate_steps", [])]
    print("\nTOOLS THE AGENT DECIDED TO USE:")
    if chain:
        print("    " + "\n      -> ".join(name + "()" for name in chain))
    else:
        print("    (none -- answered directly)")

    print("\nFINAL ANSWER:")
    print(result["output"])


DEMO_QUESTIONS = [
    "What is the name and department of student 22CS045?",
    "What are the marks of 22CS047?",
    "What is the total and average mark of 22CS045?",
    "Is 22CS045 eligible to pass according to the university rules?",
    # The challenge question -- needs all four tools.
    "I am 22CS045. Tell me my name, department, total marks, average marks, "
    "and whether I satisfy the university passing requirements.",
]


def main():
    args = [a for a in sys.argv[1:] if a not in ("--quiet", "-q")]
    verbose = "--quiet" not in sys.argv and "-q" not in sys.argv
    executor = build_agent(verbose=verbose)

    if args and args[0] == "--chat":
        print("Ask a question, or type exit to quit.")
        while True:
            try:
                question = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if question.lower() in ("exit", "quit", ""):
                break
            ask(executor, question)
        return

    if args:
        ask(executor, " ".join(args))
        return

    for question in DEMO_QUESTIONS:
        ask(executor, question)


if __name__ == "__main__":
    main()
